import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  type WAMessage,
  type WASocket,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import { config } from "./config.ts";
import { handleInbound, matchesAllowlist, registerTransport } from "./router.ts";

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
      await sock.sendMessage(chatKey.slice("wa:".length), { text });
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
      const text =
        m.message?.conversation ?? m.message?.extendedTextMessage?.text;
      if (!text) {
        // Also where an undecryptable message lands: Baileys still emits it,
        // with no plaintext to read.
        skip("no text payload");
        continue;
      }

      // WhatsApp is migrating phone JIDs (<number>@s.whatsapp.net) to LIDs
      // (<id>@lid), and which one arrives depends on the sender's client.
      // Newer Baileys carries the phone form alongside the LID in the *Pn /
      // *Alt key fields, so match on every identifier present — the same
      // number-or-ACI problem the Signal side has.
      const k = m.key as unknown as Record<string, string | undefined>;
      const bare = (j?: string): string =>
        (j ?? "").replace(/:.*$/, "").replace(/@.*$/, "");
      const ids = (
        group
          ? [k.participant, k.participantPn, k.participantAlt]
          : [k.remoteJid, k.senderPn, k.remoteJidAlt]
      )
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

      handleInbound(`wa:${jid}`, text, {
        sender: m.pushName || ids[0] || "unknown",
        allowed,
        owner,
        mentioned,
      });
    }
  });
}
