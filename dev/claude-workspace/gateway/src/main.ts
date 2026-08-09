// messaging-gateway: Signal/WhatsApp → headless `claude -p` in the
// claude-workspace $HOME. See the chart README, "Messaging surface".
import fs from "node:fs";
import { startApprovalServer } from "./approvals.ts";
import { config } from "./config.ts";
import { registerTransport, sendTo } from "./router.ts";
import { startSignal } from "./signal.ts";
import { startWhatsApp } from "./whatsapp.ts";

fs.mkdirSync(config.stateDir, { recursive: true, mode: 0o700 });
fs.mkdirSync(config.runtimeDir, { recursive: true, mode: 0o700 });

// Approval prompts go out through whichever transport owns the chat.
startApprovalServer((chatKey, text) => void sendTo(chatKey, text));

if (config.signal.enabled) {
  if (!config.signal.number || config.signal.allowedSenders.length === 0) {
    console.error(
      "signal: SIGNAL_NUMBER and SIGNAL_ALLOWED_SENDERS are required; disabling",
    );
  } else {
    startSignal();
  }
}

if (config.whatsapp.enabled) {
  if (config.whatsapp.allowedSenders.length === 0) {
    console.error("whatsapp: WA_ALLOWED_SENDERS is required; disabling");
  } else {
    await startWhatsApp();
  }
}

// Dev-only stdin transport: GW_STDIN=true lets the gateway be smoke-tested
// from a terminal with no Signal/WhatsApp pairing at all.
if (process.env.GW_STDIN === "true") {
  const { handleInbound } = await import("./router.ts");
  registerTransport("stdin", {
    chunkLimit: 100000,
    async send(_chatKey, text) {
      console.log(`\n<<< ${text}`);
    },
  });
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (line) =>
    handleInbound("stdin:local", String(line)),
  );
  console.log("stdin transport ready — type a message:");
}

console.log(
  `messaging-gateway up (signal=${config.signal.enabled}, whatsapp=${config.whatsapp.enabled})`,
);

process.on("SIGTERM", () => process.exit(0));
