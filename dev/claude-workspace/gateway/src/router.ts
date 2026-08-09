import fs from "node:fs";
import path from "node:path";
import { answerPending, hasPending } from "./approvals.ts";
import { chunkText, replyPrefix } from "./chunk.ts";
import {
  atCapacity,
  isRunning,
  latestSessionId,
  runClaude,
  stop,
} from "./claude.ts";
import { config } from "./config.ts";
import { getChat, updateChat } from "./state.ts";

export interface Transport {
  /** Chunk limit per message for this surface. */
  chunkLimit: number;
  send(chatKey: string, text: string): Promise<void>;
}

const transports = new Map<string, Transport>();

export function registerTransport(prefix: string, t: Transport): void {
  transports.set(prefix, t);
}

export async function sendTo(chatKey: string, text: string): Promise<void> {
  const t = transports.get(chatKey.split(":", 1)[0]);
  if (!t) {
    console.error(`no transport for ${chatKey}`);
    return;
  }
  for (const chunk of chunkText(text, t.chunkLimit)) {
    await t.send(chatKey, chunk);
  }
}

// One in-flight claude per chat; extra messages wait their turn.
const queues = new Map<string, string[]>();
let droppedLog = 0;

export function handleInbound(chatKey: string, text: string): void {
  const body = text.trim();
  if (!body) return;

  // Digit replies feed a pending permission prompt, never claude.
  if (hasPending(chatKey) && answerPending(chatKey, body)) return;

  if (body.startsWith("!")) {
    void handleCommand(chatKey, body);
    return;
  }

  const queue = queues.get(chatKey) ?? [];
  if (queue.length >= config.queueDepth) {
    void sendTo(chatKey, `⚠ queue full (${config.queueDepth}); message dropped`);
    return;
  }
  queue.push(body);
  queues.set(chatKey, queue);
  if (queue.length === 1 && !isRunning(chatKey)) void drain(chatKey);
}

async function drain(chatKey: string): Promise<void> {
  const queue = queues.get(chatKey);
  if (!queue?.length) return;
  if (atCapacity()) {
    // Re-check shortly; global cap bounds worst-case pod memory.
    setTimeout(() => void drain(chatKey), 3000);
    return;
  }
  const message = queue[0];
  try {
    const result = await runClaude(chatKey, message);
    const chat = getChat(chatKey);
    const prefix = replyPrefix(chat.cwd, chat.sessionId, chat.auto);
    await sendTo(
      chatKey,
      `${prefix}${result.isError ? " ⚠" : ""}\n${result.text}`,
    );
  } catch (err) {
    await sendTo(chatKey, `⚠ gateway error: ${String(err)}`);
  } finally {
    queue.shift();
    if (queue.length) void drain(chatKey);
  }
}

async function handleCommand(chatKey: string, body: string): Promise<void> {
  const [cmd, ...rest] = body.split(/\s+/);
  const arg = rest.join(" ");
  const chat = getChat(chatKey);

  switch (cmd) {
    case "!new":
      updateChat(chatKey, { sessionId: undefined });
      return sendTo(chatKey, "✓ next message starts a fresh session");
    case "!resume": {
      const id = arg || latestSessionId(chat.cwd);
      if (!id) return sendTo(chatKey, `no sessions found for ${chat.cwd}`);
      updateChat(chatKey, { sessionId: id });
      return sendTo(chatKey, `✓ resuming ${id.slice(0, 8)} in ${chat.cwd}`);
    }
    case "!cwd": {
      if (!arg) return sendTo(chatKey, `cwd: ${chat.cwd}`);
      const target = arg.startsWith("/")
        ? arg
        : path.join(config.codeRoot, arg);
      if (!fs.existsSync(target))
        return sendTo(chatKey, `⚠ no such directory: ${target}`);
      updateChat(chatKey, { cwd: target, sessionId: undefined });
      return sendTo(chatKey, `✓ cwd ${target} (session cleared)`);
    }
    case "!auto": {
      if (arg !== "on" && arg !== "off")
        return sendTo(chatKey, "usage: !auto on|off");
      updateChat(chatKey, { auto: arg === "on" });
      return sendTo(
        chatKey,
        arg === "on"
          ? "⚡ auto mode ON — tools run without asking"
          : "✓ auto mode off — mutations will prompt",
      );
    }
    case "!stop":
      return sendTo(
        chatKey,
        stop(chatKey) ? "✓ sent SIGTERM" : "nothing running",
      );
    case "!status": {
      const q = queues.get(chatKey)?.length ?? 0;
      return sendTo(
        chatKey,
        [
          `cwd: ${chat.cwd}`,
          `session: ${chat.sessionId ?? "(none)"}`,
          `auto: ${chat.auto ? "on" : "off"}`,
          `state: ${isRunning(chatKey) ? "running" : "idle"}${q ? `, ${q} queued` : ""}`,
        ].join("\n"),
      );
    }
    case "!help":
      return sendTo(
        chatKey,
        "!new · !resume [id] · !cwd <repo|path> · !auto on|off · !stop · !status\n" +
          "During a permission prompt: 1 allow · 2 deny · 3 allow all like it",
      );
    default:
      return sendTo(chatKey, `unknown command ${cmd} — try !help`);
  }
}

/** Allowlist gate. Logs (rate-limited) and drops anything unknown. */
export function isAllowed(sender: string, allowed: string[]): boolean {
  if (allowed.includes(sender)) return true;
  if (droppedLog++ % 20 === 0)
    console.warn(`dropped message from non-allowlisted sender ${sender}`);
  return false;
}
