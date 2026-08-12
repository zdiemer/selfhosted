// messaging-gateway: Signal/WhatsApp → headless `claude -p` in the
// claude-workspace $HOME. See the chart README, "Messaging surface".
import fs from "node:fs";
import { startApprovalServer } from "./approvals.ts";
import { config } from "./config.ts";
import { announceRestart, installShutdownHandler } from "./restart.ts";
import { markReady, registerTransport, sendTo } from "./transport.ts";
import { startSignal } from "./signal.ts";
import { startWhatsApp } from "./whatsapp.ts";

fs.mkdirSync(config.stateDir, { recursive: true, mode: 0o700 });
fs.mkdirSync(config.runtimeDir, { recursive: true, mode: 0o700 });

// Approval prompts go out through whichever transport owns the chat. Exits
// rather than start if another gateway already owns the socket.
await startApprovalServer((chatKey, text) => void sendTo(chatKey, text));

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
  let stdinMsgId = 0;
  registerTransport("stdin", {
    chunkLimit: 100000,
    async send(_chatKey, text) {
      const id = ++stdinMsgId;
      console.log(`\n<<< [#${id}] ${text}`);
      return id;
    },
    // Faked, but faked all the way through: the status message and the
    // reaction state machine are exercised end to end with no pairing.
    async react(_chatKey, target, emoji, remove) {
      console.log(`<<< [react ${remove ? "-" : emoji} on #${target}]`);
    },
    async edit(_chatKey, target, text) {
      console.log(`\n<<< [edit #${target}] ${text}`);
    },
  });
  markReady("stdin");
  process.stdin.setEncoding("utf8");
  let stdinInboundId = 0;
  process.stdin.on("data", (line) =>
    handleInbound("stdin:local", String(line), {
      sender: "stdin",
      allowed: true,
      owner: true,
      mentioned: true,
      // Stands in for a Signal timestamp / WhatsApp key, so the reaction state
      // machine is exercised here too rather than silently skipped.
      ref: `in${++stdinInboundId}`,
    }),
  );
  console.log("stdin transport ready — type a message:");
}

console.log(
  `messaging-gateway up (signal=${config.signal.enabled}, whatsapp=${config.whatsapp.enabled})`,
);

// Fires per surface as each one finishes connecting — sending before that is
// sending into a socket that isn't open yet.
announceRestart();
installShutdownHandler();
