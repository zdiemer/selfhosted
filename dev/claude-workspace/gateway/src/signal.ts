import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { importSignalAttachment } from "./attachments.ts";
import { config } from "./config.ts";
import { signalAuthIds, unusableSignalEntries } from "./identity.ts";
import {
  handleInbound,
  handleReaction,
  isAllowed,
  matchesAllowlist,
} from "./router.ts";
import { markReady, registerTransport } from "./transport.ts";

/** Signal addresses a message by (author, sent timestamp) — a reaction needs
 * both, an edit needs the timestamp. */
interface SignalRef {
  ts: number;
  author: string;
}

// JSON-RPC client for `signal-cli daemon --socket ...` (newline-delimited
// JSON-RPC 2.0 over a unix socket on the pod's shared /tmp emptyDir). With
// --receive-mode on-connection the daemon starts fetching as soon as we
// connect and pushes "receive" notifications down the same socket.

let sock: net.Socket | null = null;
let nextId = 1;
const pendingRpc = new Map<
  number,
  { resolve: (v: unknown) => void; reject: (e: Error) => void }
>();

function rpc(method: string, params: Record<string, unknown>): Promise<unknown> {
  return new Promise((resolve, reject) => {
    if (!sock || sock.destroyed)
      return reject(new Error("signal-cli socket not connected"));
    const id = nextId++;
    pendingRpc.set(id, { resolve, reject });
    sock.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  });
}

interface Mention {
  number?: string;
  uuid?: string;
  name?: string;
}

interface Envelope {
  sourceNumber?: string;
  sourceUuid?: string;
  source?: string;
  sourceName?: string;
  timestamp?: number;
  dataMessage?: {
    message?: string;
    reaction?: {
      emoji?: string;
      targetAuthor?: string;
      targetAuthorNumber?: string;
      targetAuthorUuid?: string;
      targetSentTimestamp?: number;
      isRemove?: boolean;
    };
    attachments?: { id?: string; filename?: string; contentType?: string }[];
    mentions?: Mention[];
    quote?: { author?: string; authorUuid?: string; authorNumber?: string };
    groupInfo?: { groupId?: string };
  };
}

// The account's own ACI. signal-cli does not expose it over JSON-RPC —
// listAccounts returns the number only — and mentions carry the ACI rather than
// the number under phone-number privacy, so without this every @-mention in a
// group silently fails to match. Read from the account store on the PVC at
// startup so a re-registration is picked up rather than hardcoded.
let selfUuid = "";

function loadSelfUuid(): void {
  const file = path.join(
    config.home,
    ".local/share/signal-cli/data/accounts.json",
  );
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8")) as {
      accounts?: { number?: string; uuid?: string }[];
    };
    selfUuid =
      data.accounts?.find((a) => a.number === config.signal.number)?.uuid ?? "";
  } catch {
    selfUuid = "";
  }
  if (selfUuid) console.log(`signal: own ACI ${selfUuid}`);
  else
    console.warn(
      "signal: could not resolve own ACI — @-mentions in groups will not match",
    );
}

/** Did this message actually tag the bot? Structured address only: a real
 * @-mention, or a reply to one of the bot's own messages. Matching the bot's
 * name in the text is not enough — in a group the name comes up in ordinary
 * conversation, and the bot answering that is exactly the failure this avoids. */
function mentionsBot(env: Envelope): boolean {
  const msg = env.dataMessage;
  const isSelf = (uuid?: string, number?: string): boolean =>
    Boolean(
      (uuid && selfUuid && uuid === selfUuid) ||
        (number && config.signal.number && number === config.signal.number),
    );

  if ((msg?.mentions ?? []).some((m) => isSelf(m.uuid, m.number))) return true;
  const q = msg?.quote;
  return Boolean(q && isSelf(q.authorUuid, q.authorNumber ?? q.author));
}

function onNotification(method: string, params: { envelope?: Envelope }): void {
  if (method !== "receive") return;
  const env = params.envelope;
  const msg = env?.dataMessage;
  // Two different lists on purpose. `ids` is everything the envelope says
  // about the sender, used for addressing and logs; `authIds` is the subset
  // that may be matched against the allowlist — the ACI only. A phone number
  // is a recyclable, SIM-swappable identifier, and this gateway is a shell on a
  // cluster-admin pod; see identity.ts.
  const ids = [env?.sourceNumber, env?.sourceUuid, env?.source].filter(
    (v): v is string => Boolean(v),
  );
  const authIds = signalAuthIds(ids);
  // Reply to the number when there is one (it keeps logs and !status readable),
  // otherwise the UUID — signal-cli accepts either as a recipient.
  const sender = ids[0];
  if (!sender) return;

  // A reaction on one of our own messages is the only non-text envelope this
  // gateway acts on — it answers an open permission prompt. Everything else
  // (receipts, typing) is still dropped.
  const reaction = msg?.reaction;
  if (reaction?.emoji && !reaction.isRemove) {
    const target = reaction.targetSentTimestamp;
    const targetIsUs =
      reaction.targetAuthorNumber === config.signal.number ||
      (Boolean(selfUuid) &&
        (reaction.targetAuthorUuid === selfUuid ||
          reaction.targetAuthor === selfUuid)) ||
      reaction.targetAuthor === config.signal.number;
    if (target && targetIsUs) {
      const groupId = msg?.groupInfo?.groupId;
      handleReaction(
        groupId ? `signal:g:${groupId}` : `signal:${sender}`,
        String(target),
        reaction.emoji,
        { owner: matchesAllowlist(authIds, config.signal.allowedSenders) },
      );
    }
    return;
  }
  // A photo with no caption is still someone showing you something.
  if (!msg?.message && !(config.attachments.enabled && msg?.attachments?.length))
    return; // no receipts/typing events

  const groupId = msg.groupInfo?.groupId;
  if (groupId && !config.groups.enabled) return;
  // An un-allowlisted group is dropped outright rather than buffered: anyone
  // can add this number to a room, and context for rooms we will never answer
  // is memory spent on strangers' conversations.
  if (groupId && !config.signal.allowedGroups.includes(groupId)) return;
  const chatKey = groupId ? `signal:g:${groupId}` : `signal:${sender}`;

  // In a group the room is the credential; in a 1:1 the sender is. `owner`
  // tracks the sender separately either way, because `!` commands need it.
  const owner = groupId
    ? matchesAllowlist(authIds, config.signal.allowedSenders)
    : isAllowed(authIds, config.signal.allowedSenders, ids);
  const allowed = groupId ? true : owner;

  const mentioned = groupId ? mentionsBot(env!) : true;

  // Acknowledge only what we're actually going to act on, so a read receipt
  // means "the bot has this", not "a packet arrived". In a group that means
  // mentions only — marking every message read would tell the room the bot is
  // reading all of it, which is true but not the impression to give.
  if (allowed && mentioned && env?.timestamp)
    void sendReadReceipt(sender, env.timestamp);

  const files = config.attachments.enabled
    ? (msg.attachments ?? [])
        .map((a) =>
          a.id ? importSignalAttachment(chatKey, a.id, a.filename) : undefined,
        )
        .filter((f): f is string => Boolean(f))
    : [];

  handleInbound(chatKey, msg.message ?? "", {
    sender: env?.sourceName || sender,
    allowed,
    owner,
    mentioned,
    // A reaction is addressed to the sender's message, so the author is the
    // sender rather than this account — the same pair the read receipt above
    // is built from.
    ref: env?.timestamp
      ? ({ ts: env.timestamp, author: env.sourceUuid ?? sender } as SignalRef)
      : undefined,
    files,
  });
}

/** Best-effort read receipt. Never let a failed ack break message handling. */
async function sendReadReceipt(
  recipient: string,
  timestamp: number,
): Promise<void> {
  try {
    await rpc("sendReceipt", {
      account: config.signal.number,
      recipient,
      targetTimestamp: [timestamp],
      type: "read",
    });
  } catch (err) {
    console.warn(`signal: read receipt failed: ${(err as Error).message}`);
  }
}

function connect(): void {
  let buf = "";
  sock = net.createConnection(config.signal.socket);

  sock.on("data", (d) => {
    buf += d.toString("utf8");
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      try {
        const m = JSON.parse(line);
        if (m.id != null && pendingRpc.has(m.id)) {
          const p = pendingRpc.get(m.id)!;
          pendingRpc.delete(m.id);
          m.error
            ? p.reject(new Error(m.error.message ?? "signal-cli rpc error"))
            : p.resolve(m.result);
        } else if (m.method) {
          onNotification(m.method, m.params ?? {});
        }
      } catch {
        console.error("signal: unparseable line from daemon");
      }
    }
  });

  const retry = () => {
    for (const p of pendingRpc.values())
      p.reject(new Error("signal-cli socket closed"));
    pendingRpc.clear();
    setTimeout(connect, 5000);
  };
  sock.once("close", retry);
  sock.on("error", (err) => console.error(`signal socket: ${err.message}`));
  sock.on("connect", () => {
    console.log("signal: connected to signal-cli daemon");
    markReady("signal");
  });
}

/** Did the send fail purely because this build can't decode images? Narrow on
 * purpose — anything else is a real failure and must not be retried into a
 * second delivery. */
export function isImageProcessingFailure(err: unknown): boolean {
  const msg = (err as Error)?.message ?? "";
  return /ImageIO|\bawt\b|UnsatisfiedLinkError/i.test(msg);
}

/** signal-cli takes `data:<type>;filename=<name>;base64,<data>` in place of a
 * path, which is the only way to say "don't treat this as an image". */
function untypedDataUri(file: string): string {
  const data = fs.readFileSync(file).toString("base64");
  return `data:application/octet-stream;filename=${path.basename(file)};base64,${data}`;
}

/** Groups are addressed by groupId, 1:1 by recipient — signal-cli takes one or
 * the other, never both. */
function addressOf(chatKey: string): Record<string, unknown> {
  const target = chatKey.slice("signal:".length);
  return target.startsWith("g:")
    ? { groupId: target.slice("g:".length) }
    : { recipient: [target] };
}

/**
 * Phone numbers left in SIGNAL_ALLOWED_SENDERS no longer authenticate anything.
 * Said loudly at startup because the alternative is a silent lockout: messages
 * from the owner would simply stop being answered, with nothing in the log
 * connecting that to the config.
 */
function warnWeakAllowlist(): void {
  const weak = unusableSignalEntries(config.signal.allowedSenders);
  if (!weak.length) return;
  console.warn(
    `signal: ignoring ${weak.length} non-ACI allowlist entr${weak.length === 1 ? "y" : "ies"} ` +
      `(${weak.join(", ")}) — only ACIs (UUIDs) authenticate; find yours in the ` +
      `daemon log's 'Envelope from: "Name" <uuid>'`,
  );
  if (unusableSignalEntries(config.signal.allowedSenders).length ===
    config.signal.allowedSenders.length)
    console.error(
      "signal: NO usable allowlist entries — every sender will be dropped until " +
        "SIGNAL_ALLOWED_SENDERS lists at least one ACI",
    );
}

export function startSignal(): void {
  loadSelfUuid();
  warnWeakAllowlist();
  registerTransport("signal", {
    // Some Signal clients truncate near 2000 chars; stay under it.
    chunkLimit: 1900,
    async send(chatKey, text) {
      const res = (await rpc("send", {
        account: config.signal.number,
        message: text,
        ...addressOf(chatKey),
      })) as { timestamp?: number } | undefined;
      // The send timestamp is the message's identity on this network — without
      // it there is nothing to edit later.
      return res?.timestamp
        ? ({ ts: res.timestamp, author: config.signal.number } as SignalRef)
        : undefined;
    },
    async react(chatKey, target, emoji, remove) {
      const r = target as SignalRef;
      await rpc("sendReaction", {
        account: config.signal.number,
        emoji,
        targetAuthor: r.author,
        targetTimestamp: r.ts,
        ...(remove ? { remove: true } : {}),
        ...addressOf(chatKey),
      });
    },
    refId(ref) {
      return String((ref as SignalRef).ts);
    },
    async sendFile(chatKey, file, caption) {
      // signal-cli's `send --attachment`; the caption rides along as the
      // message body, which is how Signal renders it.
      const params = {
        account: config.signal.number,
        message: caption,
        ...addressOf(chatKey),
      };
      try {
        await rpc("send", { ...params, attachment: [file] });
      } catch (err) {
        if (!isImageProcessingFailure(err)) throw err;
        // The GraalVM native signal-cli build ships no ImageIO/AWT, and
        // signal-cli only reaches for them when the attachment is typed as an
        // image — to read its dimensions. Re-send declaring a generic type:
        // the bytes are identical and Signal still previews it inline, it just
        // arrives without dimensions attached.
        console.warn(
          `signal: no ImageIO in this build; re-sending ${file} untyped`,
        );
        await rpc("send", {
          ...params,
          attachment: [untypedDataUri(file)],
        });
      }
    },
    // An edit is one local socket call to signal-cli, so the constraint here
    // is Signal's ten-revisions-per-message cap rather than the volume
    // concerns that hold WhatsApp back.
    minEditIntervalMs: config.progress.signalMinIntervalMs,
    async edit(chatKey, target, text) {
      // signal-cli's `send --edit-timestamp`, targeting the LATEST revision:
      // each edit is its own message with its own timestamp, and the next one
      // has to chain off that. Targeting the original across every edit — what
      // this did until 2026-08-14 — lands the first revision or two and is then
      // quietly ignored, so the status froze while the run carried on. Verified
      // against this account: ten edits chained all landed, ten aimed at the
      // root did not.
      const res = (await rpc("send", {
        account: config.signal.number,
        message: text,
        editTimestamp: (target as SignalRef).ts,
        ...addressOf(chatKey),
      })) as { timestamp?: number } | undefined;
      return res?.timestamp
        ? ({ ts: res.timestamp, author: config.signal.number } as SignalRef)
        : undefined;
    },
  });
  connect();
}
