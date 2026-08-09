import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  type WASocket,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import { config } from "./config.ts";
import { handleInbound, isAllowed, registerTransport } from "./router.ts";

// Baileys speaks the real WhatsApp Web protocol over an outbound websocket —
// no ingress, no webhook. Auth keys persist on the PVC so a pod restart
// reconnects without re-pairing. Unofficial client: keep volume low, use a
// dedicated number (see chart README, "ban risk").

let sock: WASocket | null = null;

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
  sock = makeWASocket({ auth: state, printQRInTerminal: false });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    if (u.qr) {
      // Pairing: scan from `kubectl logs -c messaging-gateway`.
      console.log("whatsapp: scan this QR in WhatsApp → Linked Devices:");
      qrcode.generate(u.qr, { small: true });
    }
    if (u.connection === "open") console.log("whatsapp: connected");
    if (u.connection === "close") {
      const code = (u.lastDisconnect?.error as any)?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.error(
          "whatsapp: logged out — delete wa-auth/ and re-pair to recover",
        );
        return;
      }
      console.warn(`whatsapp: connection closed (${code}); reconnecting`);
      setTimeout(() => void connect(), 5000);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      const jid = m.key.remoteJid;
      // Direct chats only; skip groups, own messages, and non-text payloads.
      if (!jid?.endsWith("@s.whatsapp.net") || m.key.fromMe) continue;
      const text =
        m.message?.conversation ?? m.message?.extendedTextMessage?.text;
      if (!text) continue;
      const senderNumber = jid.replace("@s.whatsapp.net", "");
      if (!isAllowed(senderNumber, config.whatsapp.allowedSenders)) continue;
      handleInbound(`wa:${jid}`, text);
    }
  });
}
