import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { config } from "./config.ts";
import {
  handleInbound,
  isAllowed,
  matchesAllowlist,
  registerTransport,
} from "./router.ts";

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
  // Signal identifies a sender by ACI (UUID) and includes `sourceNumber` only
  // when they share their phone number — phone-number privacy has been the
  // default since 2024, so for most senders it is simply absent. Check every
  // identifier the envelope carries against the allowlist instead of picking
  // one, so an E.164 number and a UUID are both valid config.
  const ids = [env?.sourceNumber, env?.sourceUuid, env?.source].filter(
    (v): v is string => Boolean(v),
  );
  // Reply to the number when there is one (it keeps logs and !status readable),
  // otherwise the UUID — signal-cli accepts either as a recipient.
  const sender = ids[0];
  if (!msg?.message || !sender) return; // no receipts/typing events

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
    ? matchesAllowlist(ids, config.signal.allowedSenders)
    : isAllowed(ids, config.signal.allowedSenders);
  const allowed = groupId ? true : owner;

  const mentioned = groupId ? mentionsBot(env!) : true;

  // Acknowledge only what we're actually going to act on, so a read receipt
  // means "the bot has this", not "a packet arrived". In a group that means
  // mentions only — marking every message read would tell the room the bot is
  // reading all of it, which is true but not the impression to give.
  if (allowed && mentioned && env?.timestamp)
    void sendReadReceipt(sender, env.timestamp);

  handleInbound(chatKey, msg.message, {
    sender: env?.sourceName || sender,
    allowed,
    owner,
    mentioned,
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
  sock.on("connect", () => console.log("signal: connected to signal-cli daemon"));
}

export function startSignal(): void {
  loadSelfUuid();
  registerTransport("signal", {
    // Some Signal clients truncate near 2000 chars; stay under it.
    chunkLimit: 1900,
    async send(chatKey, text) {
      const target = chatKey.slice("signal:".length);
      // Groups are addressed by groupId, 1:1 by recipient — signal-cli takes
      // one or the other, never both.
      await rpc("send", {
        account: config.signal.number,
        message: text,
        ...(target.startsWith("g:")
          ? { groupId: target.slice("g:".length) }
          : { recipient: [target] }),
      });
    },
  });
  connect();
}
