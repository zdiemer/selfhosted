import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  type WAMessage,
  type WASocket,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import { saveInbound } from "./attachments.ts";
import { config } from "./config.ts";
import { handleInbound, handleReaction, matchesAllowlist } from "./router.ts";
import { markReady, registerTransport } from "./transport.ts";

// Baileys speaks the real WhatsApp Web protocol over an outbound websocket —
// no ingress, no webhook. Auth keys persist on the PVC so a pod restart
// reconnects without re-pairing. Unofficial client: keep volume low, use a
// dedicated number (see chart README, "ban risk").
//
// ⚠️ baileys is pinned EXACTLY in package.json — never restore the caret.
// Upstream published 6.17.16 on 2025-03-04, after the 6.7.x line, so semver
// ranks it above the genuinely newer 6.7.24 (2026-07-29) and `^6.7.x` resolves
// *backwards* to a year-old client. WhatsApp refuses that client's stale
// protocol version at the handshake with a 405, before pairing ever starts.

let sock: WASocket | null = null;

const jidOf = (chatKey: string): string => chatKey.slice("wa:".length);

/** WhatsApp gives a mimetype and no name for a camera photo; claude reads by
 * extension, so give the file one. */
const MIME_BY_EXT: Record<string, string> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".pdf": "application/pdf",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".json": "application/json",
  ".csv": "text/csv",
  ".svg": "image/svg+xml",
  ".zip": "application/zip",
  ".mp4": "video/mp4",
  ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg",
};

function mimetypeFor(file: string): string {
  return MIME_BY_EXT[file.slice(file.lastIndexOf(".")).toLowerCase()] ?? "";
}

function extensionFor(mimetype?: string | null): string {
  const map: Record<string, string> = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
    "text/plain": ".txt",
  };
  return map[(mimetype ?? "").split(";")[0]] ?? "";
}

/** Did this message actually tag the bot? Structured address only: a real
 * @-mention, or a reply to one of the bot's own messages. A name match in the
 * text does not count — the name comes up in ordinary group conversation. */
function mentionsBot(m: WAMessage): boolean {
  const ctx = m.message?.extendedTextMessage?.contextInfo;
  if (!ctx) return false;
  // Strip the device suffix (`:12`) and domain; WhatsApp addresses the account
  // by phone JID or LID depending on the sender's client.
  const ids = [sock?.user?.id, sock?.user?.lid]
    .filter((v): v is string => Boolean(v))
    .map((v) => v.replace(/:.*$/, "").replace(/@.*$/, ""));
  if (!ids.length) return false;
  const bare = (jid: string): string => jid.replace(/:.*$/, "").replace(/@.*$/, "");
  if (ctx.mentionedJid?.some((j) => ids.includes(bare(j)))) return true;
  return Boolean(ctx.participant && ids.includes(bare(ctx.participant)));
}

/** Best-effort read receipt; a failed ack must not drop the message. */
async function markRead(key: WAMessage["key"]): Promise<void> {
  try {
    await sock?.readMessages([key]);
  } catch (err) {
    console.warn(`whatsapp: read receipt failed: ${(err as Error).message}`);
  }
}

// Reconnect backoff. A fixed 5s retry against a server-side rejection is a
// reconnect storm from one egress IP — the likeliest way to get this number or
// address blocked before it has sent a single message.
const RECONNECT_MIN_MS = 5_000;
const RECONNECT_MAX_MS = 300_000;
let reconnectDelay = RECONNECT_MIN_MS;

export async function startWhatsApp(): Promise<void> {
  registerTransport("wa", {
    chunkLimit: 2900,
    async send(chatKey, text) {
      if (!sock) throw new Error("whatsapp not connected");
      const sent = await sock.sendMessage(jidOf(chatKey), { text });
      // The key is this message's identity; an edit or reaction is addressed
      // to it. Baileys returns undefined if the send was swallowed.
      return sent?.key;
    },
    async react(chatKey, target, emoji, remove) {
      if (!sock) throw new Error("whatsapp not connected");
      // An empty reaction text is how WhatsApp expresses "take mine back".
      await sock.sendMessage(jidOf(chatKey), {
        react: { text: remove ? "" : emoji, key: target as WAMessage["key"] },
      });
    },
    async sendFile(chatKey, file, caption) {
      if (!sock) throw new Error("whatsapp not connected");
      // WhatsApp renders these very differently: an image inline, anything
      // else as a file card. Sending a screenshot as a document would defeat
      // the point of sending it at all.
      const ext = file.slice(file.lastIndexOf(".")).toLowerCase();
      const image = [".png", ".jpg", ".jpeg", ".gif", ".webp"].includes(ext);
      await sock.sendMessage(
        jidOf(chatKey),
        image
          ? { image: { url: file }, caption: caption || undefined }
          : {
              document: { url: file },
              mimetype: mimetypeFor(file) || "application/octet-stream",
              fileName: file.slice(file.lastIndexOf("/") + 1),
              caption: caption || undefined,
            },
      );
    },
    refId(ref) {
      return (ref as WAMessage["key"]).id ?? undefined;
    },
    async edit(chatKey, target, text) {
      if (!sock) throw new Error("whatsapp not connected");
      // WhatsApp refuses edits more than ~15 minutes after the original, so on
      // a long run the status message stops updating partway. transport.ts
      // treats that as a failed best-effort edit, which is the right outcome:
      // the answer still arrives as its own message.
      await sock.sendMessage(jidOf(chatKey), {
        text,
        edit: target as WAMessage["key"],
      });
    },
  });
  await connect();
}

async function connect(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(
    path.join(config.stateDir, "wa-auth"),
  );
  // Ask WhatsApp which protocol version it is currently serving. The version
  // baked into the library goes stale between releases, and a stale one is
  // refused with the same 405 as an outdated client — so pinning the package
  // alone is not enough to stay connectable.
  let version: [number, number, number] | undefined;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch (err) {
    console.warn(
      `whatsapp: could not fetch the current WA version (${(err as Error).message}); falling back to the library default`,
    );
  }

  sock = makeWASocket({ auth: state, version, printQRInTerminal: false });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    if (u.qr) {
      // Pairing: scan from `kubectl logs -c messaging-gateway`.
      console.log("whatsapp: scan this QR in WhatsApp → Linked Devices:");
      qrcode.generate(u.qr, { small: true });
    }
    if (u.connection === "open") {
      console.log("whatsapp: connected");
      reconnectDelay = RECONNECT_MIN_MS;
      markReady("wa");
    }
    if (u.connection === "close") {
      const code = (u.lastDisconnect?.error as any)?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.error(
          "whatsapp: logged out — delete wa-auth/ and re-pair to recover",
        );
        return;
      }
      if (code === DisconnectReason.restartRequired) {
        // WhatsApp closes the socket with 515 the moment pairing succeeds and
        // expects an immediate reconnect. That is the handshake completing, not
        // a failure, so it must neither wait nor consume a backoff step —
        // pairing after a run of failures would otherwise stall for the full
        // 5-minute cap at the exact moment it started working.
        console.log("whatsapp: restart required after pairing; reconnecting");
        setTimeout(() => void connect(), 1_000);
        return;
      }
      const wait = reconnectDelay;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      console.warn(
        `whatsapp: connection closed (${code}); reconnecting in ${Math.round(wait / 1000)}s`,
      );
      setTimeout(() => void connect(), wait);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    // History-sync batches arrive as "append" and are not live traffic.
    if (type !== "notify") {
      console.log(`wa: ignoring ${messages.length} ${type} message(s)`);
      return;
    }
    for (const m of messages) {
      const jid = m.key.remoteJid;
      // Every branch below used to drop the message silently, which made "the
      // bot ignored me" indistinguishable from "nothing ever arrived" — the
      // failure this surface has now hit on both networks. Say why, always.
      const skip = (reason: string): void =>
        console.log(`wa: skip (${reason}) jid=${jid ?? "?"}`);

      if (!jid) {
        skip("no remoteJid");
        continue;
      }
      if (m.key.fromMe) {
        skip("own message");
        continue;
      }
      // A reaction on one of OUR messages answers an open permission prompt.
      // It arrives as an ordinary upsert whose payload is a reactionMessage,
      // so it has to be handled before the "no text payload" skip below.
      const reaction = m.message?.reactionMessage;
      if (reaction?.text && reaction.key?.fromMe && reaction.key.id) {
        const rk = m.key as unknown as Record<string, string | undefined>;
        const rbare = (j?: string): string =>
          (j ?? "").replace(/:.*$/, "").replace(/@.*$/, "");
        // Session identity only — the *Pn / *Alt aliases are excluded, see the
        // note on `ids` below.
        const rids = (
          jid.endsWith("@g.us") ? [rk.participant] : [rk.remoteJid]
        )
          .map(rbare)
          .filter(Boolean);
        handleReaction(`wa:${jid}`, reaction.key.id, reaction.text, {
          owner: matchesAllowlist(rids, config.whatsapp.allowedSenders),
        });
        continue;
      }
      const group = jid.endsWith("@g.us");
      // A DM arrives as a phone JID or as a LID depending on the sender's
      // client. Anything else — channels, broadcasts, status — is not a chat.
      if (!group && !jid.endsWith("@s.whatsapp.net") && !jid.endsWith("@lid")) {
        skip("not a direct chat or group");
        continue;
      }
      if (group && !config.groups.enabled) {
        skip("groups disabled");
        continue;
      }
      // Un-allowlisted rooms are dropped outright, not buffered — anyone can
      // add this number to a group.
      if (group && !config.whatsapp.allowedGroups.includes(jid)) {
        skip("group not allowlisted");
        continue;
      }
      const media =
        m.message?.imageMessage ??
        m.message?.documentMessage ??
        m.message?.videoMessage ??
        m.message?.audioMessage;
      const text =
        m.message?.conversation ??
        m.message?.extendedTextMessage?.text ??
        (media as { caption?: string } | undefined)?.caption ??
        "";
      if (!text && !(config.attachments.enabled && media)) {
        // Also where an undecryptable message lands: Baileys still emits it,
        // with no plaintext to read.
        skip("no text payload");
        continue;
      }

      // WhatsApp is migrating phone JIDs (<number>@s.whatsapp.net) to LIDs
      // (<id>@lid), and which one arrives depends on the sender's client. Only
      // the JID the encrypted session is actually with — remoteJid in a DM,
      // participant in a group — authenticates here.
      //
      // The *Pn / *Alt siblings are excluded deliberately, and they used to be
      // matched. They are WhatsApp's *claim* about which other identity belongs
      // to the same person, not something the sender proves: trusting them
      // means a mis-mapped (or maliciously mapped) LID→number pair admits an
      // arbitrary account to a cluster-admin shell. Matching admits on ANY id,
      // so the weakest one in the list is the real gate. See identity.ts.
      //
      // Consequence for config: WA_ALLOWED_SENDERS must list whichever form
      // this sender's client actually sends. If a client moves to LIDs the
      // skip log below prints the id that arrived — list that.
      const k = m.key as unknown as Record<string, string | undefined>;
      const bare = (j?: string): string =>
        (j ?? "").replace(/:.*$/, "").replace(/@.*$/, "");
      const ids = (group ? [k.participant] : [k.remoteJid])
        .map(bare)
        .filter(Boolean);

      // In a group the room is the credential; the sender still decides
      // `owner`, which is what gates `!` commands.
      const owner = matchesAllowlist(ids, config.whatsapp.allowedSenders);
      const allowed = group ? true : owner;
      if (!allowed) {
        skip(`sender not allowlisted (ids: ${ids.join(",") || "none"})`);
        continue;
      }

      const mentioned = group ? mentionsBot(m) : true;
      // Only acknowledge what the bot will act on — in a group, mentions only.
      if (mentioned) void markRead(m.key);

      // Downloading is async and the upsert handler is not, so the run starts
      // once the bytes are on disk rather than racing them.
      void (async () => {
        const files: string[] = [];
        if (config.attachments.enabled && media && mentioned) {
          try {
            const data = (await downloadMediaMessage(m, "buffer", {})) as Buffer;
            const name =
              (media as { fileName?: string }).fileName ??
              `${m.key.id ?? "media"}${extensionFor(media.mimetype)}`;
            const saved = saveInbound(`wa:${jid}`, name, data);
            if (saved) files.push(saved);
          } catch (err) {
            console.warn(`wa: media download failed: ${(err as Error).message}`);
          }
        }
        handleInbound(`wa:${jid}`, text, {
          sender: m.pushName || ids[0] || "unknown",
          allowed,
          owner,
          mentioned,
          // Same key the read receipt above is addressed to; a reaction on the
          // sender's message needs it.
          ref: m.key,
          files,
        });
      })();
    }
  });
}
