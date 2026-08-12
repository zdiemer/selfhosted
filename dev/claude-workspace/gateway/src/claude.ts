import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { isGroupChat } from "./chat.ts";
import { approvalSocketPath, config } from "./config.ts";
import { autoActive, type ChatState, getChat, updateChat } from "./state.ts";

export interface RunResult {
  text: string;
  sessionId?: string;
  isError: boolean;
}

/** One NDJSON line from `--output-format stream-json`. Only the fields this
 * gateway reads are typed; the stream carries a good deal more. */
export interface StreamEvent {
  type?: string;
  subtype?: string;
  session_id?: string;
  message?: {
    content?: {
      type?: string;
      text?: string;
      name?: string;
      input?: Record<string, unknown>;
    }[];
  };
  result?: string;
  is_error?: boolean;
  /** Present on the `result` event. Drives the token/cost tail on the status
   * receipt, so a run's cost is visible without reaching for !usage. */
  total_cost_usd?: number;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_creation_input_tokens?: number;
    cache_read_input_tokens?: number;
  };
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

/** Chats with a claude running right now — the ones a shutdown owes a word to. */
export function runningChats(): string[] {
  return [...running.keys()];
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
  onEvent?: (ev: StreamEvent) => void,
): Promise<RunResult> {
  const chat: ChatState = getChat(chatKey);
  const group = isGroupChat(chatKey);
  const prompt = contextPrefix ? `${contextPrefix}\n\n${message}` : message;
  // stream-json rather than json: the events are what drive the live status
  // message, and the init event carries session_id early enough to persist it
  // before the run can be interrupted. --verbose is required alongside it.
  const args = [
    "-p",
    prompt,
    "--output-format",
    "stream-json",
    "--verbose",
  ];
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
  } else if (autoActive(chat) && !chat.plan) {
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
  // Recorded on the PVC so the next boot can tell this chat its run was cut
  // off mid-flight (main.ts). Cleared in the finally below, including on crash.
  updateChat(chatKey, { inFlight: true });
  try {
    return await new Promise<RunResult>((resolve) => {
      const child = spawn("claude", args, {
        cwd: chat.cwd,
        env: { ...process.env, HOME: config.home },
        stdio: ["ignore", "pipe", "pipe"],
      });
      running.set(chatKey, child);

      let err = "";
      let result: RunResult | null = null;
      let sessionId: string | undefined;

      // NDJSON: the same line-split loop the signal-cli socket uses, because a
      // chunk boundary lands mid-line often enough to matter.
      let buf = "";
      child.stdout!.on("data", (d) => {
        buf += d;
        let nl: number;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl);
          buf = buf.slice(nl + 1);
          if (line.trim()) handleLine(line);
        }
      });
      child.stderr!.on("data", (d) => (err += d));

      const handleLine = (line: string): void => {
        let ev: StreamEvent;
        try {
          ev = JSON.parse(line) as StreamEvent;
        } catch {
          // A non-JSON line on stdout is a claude-side warning, not a fatal
          // condition. Keep it for the error path and carry on.
          err += line + "\n";
          return;
        }
        // Persist the session the moment it is known, not at the end. This is
        // what makes a run killed by a redeploy resumable: the transcript is
        // already on the PVC, we just have to remember its id.
        if (ev.session_id && ev.session_id !== sessionId) {
          sessionId = ev.session_id;
          updateChat(chatKey, { sessionId });
        }
        if (ev.type === "result") {
          result = {
            text: ev.result ?? "(no result text)",
            sessionId: ev.session_id ?? sessionId,
            isError: Boolean(ev.is_error),
          };
        }
        try {
          onEvent?.(ev);
        } catch (e) {
          // A broken progress renderer must not take the run down with it.
          console.warn(`onEvent failed: ${(e as Error).message}`);
        }
      };

      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        err += `\n(timed out after ${config.claudeTimeoutMs / 60000}m)`;
      }, config.claudeTimeoutMs);

      child.on("close", (code) => {
        clearTimeout(timer);
        running.delete(chatKey);
        if (buf.trim()) handleLine(buf);
        // No result event means the run died before finishing — killed by
        // !stop, the timeout, or a SIGTERM from a redeploy. Say what happened
        // rather than hand back an empty answer.
        resolve(
          result ?? {
            text:
              `claude exited ${code}` +
              (err.trim() ? `\n${err.trim().slice(-800)}` : ""),
            sessionId,
            isError: true,
          },
        );
      });
    });
  } finally {
    activeCount--;
    updateChat(chatKey, { inFlight: false });
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
