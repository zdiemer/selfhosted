import net from "node:net";
import { config } from "./config.ts";
import { handleInbound, isAllowed, registerTransport } from "./router.ts";

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

interface Envelope {
  sourceNumber?: string;
  source?: string;
  dataMessage?: { message?: string; groupInfo?: unknown };
}

function onNotification(method: string, params: { envelope?: Envelope }): void {
  if (method !== "receive") return;
  const env = params.envelope;
  const msg = env?.dataMessage;
  const sender = env?.sourceNumber ?? env?.source;
  // Text-only, direct chats only: no groups (v1), no receipts/typing events.
  if (!msg?.message || msg.groupInfo || !sender) return;
  if (!isAllowed(sender, config.signal.allowedSenders)) return;
  handleInbound(`signal:${sender}`, msg.message);
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
  registerTransport("signal", {
    // Some Signal clients truncate near 2000 chars; stay under it.
    chunkLimit: 1900,
    async send(chatKey, text) {
      const recipient = chatKey.slice("signal:".length);
      await rpc("send", {
        account: config.signal.number,
        recipient: [recipient],
        message: text,
      });
    },
  });
  connect();
}
