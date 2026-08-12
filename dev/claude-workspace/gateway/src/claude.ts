import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { isGroupChat } from "./chat.ts";
import { approvalSocketPath, config } from "./config.ts";
import { type ChatState, getChat, updateChat } from "./state.ts";

export interface RunResult {
  text: string;
  sessionId?: string;
  isError: boolean;
}

const running = new Map<string, ReturnType<typeof spawn>>();
let activeCount = 0;

export function isRunning(chatKey: string): boolean {
  return running.has(chatKey);
}

export function stop(chatKey: string): boolean {
  const child = running.get(chatKey);
  if (!child) return false;
  child.kill("SIGTERM");
  return true;
}

export function atCapacity(): boolean {
  return activeCount >= config.maxConcurrentClaude;
}

// Each chat gets its own mcp config so approve-mcp knows which chat to ask.
function mcpConfigPath(chatKey: string): string {
  const safe = chatKey.replace(/[^A-Za-z0-9]+/g, "-");
  const p = path.join(config.runtimeDir, `mcp-${safe}.json`);
  fs.mkdirSync(config.runtimeDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(
    p,
    JSON.stringify({
      mcpServers: {
        gw: {
          command: "bun",
          args: [path.join(config.appDir, "src/approve-mcp.ts")],
          env: { GW_SOCKET: approvalSocketPath, GW_CHAT_KEY: chatKey },
        },
      },
    }),
    { mode: 0o600 },
  );
  return p;
}

export async function runClaude(
  chatKey: string,
  message: string,
  contextPrefix = "",
): Promise<RunResult> {
  const chat: ChatState = getChat(chatKey);
  const group = isGroupChat(chatKey);
  const prompt = contextPrefix ? `${contextPrefix}\n\n${message}` : message;
  const args = ["-p", prompt, "--output-format", "json"];
  args.push("--model", chat.model ?? config.model);
  args.push("--effort", chat.effort ?? config.effort);
  // Append rather than replace: Claude Code's own system prompt is what tells
  // it which tools exist. This only adds what it can't know — that the far end
  // is a phone, not a terminal.
  const systemPrompt = group
    ? `${config.systemPrompt}\n\n${config.groups.systemPrompt}`
    : config.systemPrompt;
  if (systemPrompt) args.push("--append-system-prompt", systemPrompt);
  if (chat.sessionId) args.push("--resume", chat.sessionId);
  if (group) {
    // Groups never get auto mode and never get an interactive prompt: the room
    // reads every reply, and only one member is even on the allowlist to
    // answer. The approval relay is still wired up, but approvals.ts denies
    // outright for a group key — so this restricted set is a hard ceiling
    // rather than the starting point of a negotiation.
    args.push(
      "--permission-prompt-tool",
      "mcp__gw__approve",
      "--mcp-config",
      mcpConfigPath(chatKey),
      "--strict-mcp-config",
      "--allowedTools",
      config.groups.allowedTools,
    );
  } else if (chat.auto && !chat.plan) {
    args.push("--permission-mode", "bypassPermissions");
  } else {
    // --strict-mcp-config keeps the headless run from loading the workspace's
    // interactive MCP servers; only the approval relay is wired in.
    args.push(
      "--permission-prompt-tool",
      "mcp__gw__approve",
      "--mcp-config",
      mcpConfigPath(chatKey),
      "--strict-mcp-config",
      "--allowedTools",
      config.allowedTools,
    );
    // Plan mode outranks auto (the router keeps them from being set together,
    // but state written by an older build could have both). Claude refuses
    // edits itself here; the approval relay still covers the rest, and an
    // ExitPlanMode arrives as an ordinary "reply 1/2/3" prompt.
    if (chat.plan) args.push("--permission-mode", "plan");
  }

  activeCount++;
  try {
    return await new Promise<RunResult>((resolve) => {
      const child = spawn("claude", args, {
        cwd: chat.cwd,
        env: { ...process.env, HOME: config.home },
        stdio: ["ignore", "pipe", "pipe"],
      });
      running.set(chatKey, child);

      let out = "";
      let err = "";
      child.stdout!.on("data", (d) => (out += d));
      child.stderr!.on("data", (d) => (err += d));

      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        err += `\n(timed out after ${config.claudeTimeoutMs / 60000}m)`;
      }, config.claudeTimeoutMs);

      child.on("close", (code) => {
        clearTimeout(timer);
        running.delete(chatKey);
        try {
          const parsed = JSON.parse(out);
          if (parsed.session_id)
            updateChat(chatKey, { sessionId: parsed.session_id });
          resolve({
            text: parsed.result ?? "(no result text)",
            sessionId: parsed.session_id,
            isError: Boolean(parsed.is_error),
          });
        } catch {
          resolve({
            text:
              `claude exited ${code}` +
              (err.trim() ? `\n${err.trim().slice(-800)}` : ""),
            isError: true,
          });
        }
      });
    });
  } finally {
    activeCount--;
  }
}

/** Newest session jsonl for a cwd — claude names project dirs by munging
 * '/' and '.' to '-'. Used by !resume to pick up a tmux/Happy session. */
export function latestSessionId(cwd: string): string | undefined {
  const projectDir = path.join(
    config.home,
    ".claude/projects",
    cwd.replace(/[/.]/g, "-"),
  );
  try {
    const newest = fs
      .readdirSync(projectDir)
      .filter((f) => f.endsWith(".jsonl"))
      .map((f) => ({
        id: f.replace(/\.jsonl$/, ""),
        mtime: fs.statSync(path.join(projectDir, f)).mtimeMs,
      }))
      .sort((a, b) => b.mtime - a.mtime)[0];
    return newest?.id;
  } catch {
    return undefined;
  }
}
