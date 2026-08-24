import { type ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { isGroupChat } from "./chat.ts";
import { approvalSocketPath, config } from "./config.ts";
import {
  type ChatState,
  getChat,
  resumeIdForSlot,
  sessionPatchForSlot,
  updateChat,
} from "./state.ts";

/** Per-run overrides for a schedule-fired run (schedules.ts): a pinned cwd
 * and session slot, so a recurring job never runs in — or writes its session
 * id over — whatever the chat happens to be pointed at. */
export interface RunOverrides {
  cwd?: string;
  /** Named session slot to resume and to record new ids under. */
  session?: string;
  /** Start fresh each firing; the new id still lands in the slot. */
  fresh?: boolean;
  model?: string;
  effort?: string;
}

export interface RunResult {
  text: string;
  sessionId?: string;
  isError: boolean;
  /** What this turn's confirmed ScheduleWakeup call asked for, if it made one.
   * The tool's own timer lives in the claude child and dies with it (verified:
   * with stdin closed the CLI exits the moment the turn ends, wake-up armed or
   * not), so the gateway re-arms the timer on its side of the process boundary
   * (wakeup.ts). "stop" means the model ended its loop. */
  wakeup?: { at: number; prompt: string } | "stop";
  /** The process ended without a result event — !stop, the timeout, a
   * redeploy's SIGTERM, or a crash. No turn was reported for it, so the caller
   * is the only one who can collapse the status and say something. */
  aborted?: boolean;
}

/** A background task the CLI is still carrying — a `run_in_background` Bash,
 * a backgrounded subagent. The gateway only needs enough to say what it is
 * waiting on; the CLI owns everything else about them. */
export interface BackgroundTask {
  task_id?: string;
  task_type?: string;
  description?: string;
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
      /** tool_use blocks: the id its tool_result answers to. */
      id?: string;
      /** tool_result blocks (type "user" events). */
      tool_use_id?: string;
      is_error?: boolean;
    }[];
  };
  result?: string;
  is_error?: boolean;
  /** `system/background_tasks_changed` carries the WHOLE outstanding list each
   * time, so this replaces what we knew rather than amending it. Empty means
   * nothing is outstanding. */
  tasks?: BackgroundTask[];
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

/** Why the gateway stopped waiting on background work that never reported. */
export type AbandonReason = "timeout" | "turns";

export interface RunHooks {
  onEvent?(ev: StreamEvent): void;
  /** The child's own ScheduleWakeup timer just fired in-process (it was parked
   * on background work when the moment came, so it was still alive to do it).
   * The gateway's persisted copy of that wake-up is spent and must be
   * discarded, or the same prompt fires a second time from the outside. */
  onSelfWake?(): void;
  /**
   * One completed turn. Called for the answer to the message that started the
   * run AND for every later turn the CLI wakes itself into — a background task
   * finishing, a message handed to the parked run. `pending` is the background
   * work still outstanding at that moment, which is what makes this turn's
   * reply "…and I'll report back" rather than the last word.
   *
   * Awaited, and chained: two turns' replies must not interleave on the wire.
   */
  onTurn?(result: RunResult, turn: number, pending: BackgroundTask[]): Promise<void>;
  /** The wait ran out (or the turn budget did) with work still outstanding.
   * Those tasks die with the process, so this is the only warning there is. */
  onAbandon?(tasks: BackgroundTask[], reason: AbandonReason): void;
}

/**
 * A claude process that is up. It outlives its first answer: the CLI kills its
 * own background tasks on shutdown, so the only way a `run_in_background` Bash
 * or a backgrounded subagent ever reports back is if this process is still
 * here when it does.
 */
interface LiveRun {
  child: ChildProcess;
  /** Outstanding background work, as of the last `background_tasks_changed`. */
  tasks: BackgroundTask[];
  /** A `task_notification` landed and no turn has started to consume it yet.
   * The CLI clears the task list just BEFORE it wakes itself, so the list
   * alone would have us close stdin in the gap and lose the wake. */
  wakePending: boolean;
  turns: number;
  /** Parked: the last turn is answered, stdin is still open, and we are
   * waiting for the CLI to wake itself. Idle, not busy — which is why a new
   * message can be handed straight to it (handOff). */
  waiting: boolean;
  /** Gives up on a wake that never comes. */
  idle: ReturnType<typeof setTimeout> | null;
  /** Epoch ms of the ScheduleWakeup timer this child holds internally, if its
   * stream confirmed one. While the child is alive, ITS timer is the real one
   * — the gateway's copy (wakeup.ts) is a crash fallback, and defers to this
   * (hasLiveWakeup) rather than fire the same prompt twice. */
  wakeupAt: number | null;
}

const running = new Map<string, LiveRun>();
let activeCount = 0;

/** `--input-format stream-json` speaks the same NDJSON as the output: one JSON
 * user message per line. Writing rather than passing `-p <prompt>` is what
 * keeps stdin open, and an open stdin is what keeps the process alive past its
 * first answer. */
function userMessage(text: string): string {
  return (
    JSON.stringify({ type: "user", message: { role: "user", content: text } }) +
    "\n"
  );
}

/** Text blocks of an assistant event, joined. Empty for tool-only events. */
export function assistantText(ev: StreamEvent): string {
  if (ev.type !== "assistant") return "";
  return (ev.message?.content ?? [])
    .filter((b) => b.type === "text" && b.text)
    .map((b) => b.text)
    .join("\n")
    .trim();
}

/** The result event's text is empty when the turn ended on a tool call
 * (a scheduled wake-up, a background handoff) — the words the model wrote
 * before that call are in an earlier assistant event, not in `result`.
 * Empty when the turn said nothing at all; the router decides whether that
 * deserves a message (it does not, for a wake nobody asked for). */
export function pickResultText(
  result: string | undefined,
  lastAssistant: string,
): string {
  if (result?.trim()) return result;
  return lastAssistant;
}

/**
 * Watches one run's stream for ScheduleWakeup calls, so the gateway can take
 * over the timer the exiting child process would otherwise drop.
 *
 * Only a CONFIRMED call counts — the tool_use must be answered by a
 * tool_result without is_error, or a call the permission relay denied (groups
 * deny everything but web tools) would arm a wake-up the model was refused.
 * A later confirmed call replaces an earlier one and `stop: true` cancels,
 * which is the harness's own contract for the tool.
 *
 * take() empties the tracker, so each turn reports only what IT armed: a
 * wake turn that never mentions ScheduleWakeup must not re-arm the previous
 * turn's (already spent) wake-up.
 */
export class WakeupTracker {
  private calls = new Map<string, Record<string, unknown>>();
  private state: RunResult["wakeup"];
  /** The wake-up the CHILD currently holds a live timer for, across turns.
   * Unlike `state` this is not consumed by take(): it describes the process,
   * not the turn. */
  pendingAt: number | null = null;

  constructor(private now: () => number = Date.now) {}

  onEvent(ev: StreamEvent): void {
    for (const block of ev.message?.content ?? []) {
      if (
        ev.type === "assistant" &&
        block.type === "tool_use" &&
        block.name === "ScheduleWakeup" &&
        block.id
      ) {
        this.calls.set(block.id, block.input ?? {});
      } else if (
        ev.type === "user" &&
        block.type === "tool_result" &&
        block.tool_use_id
      ) {
        const input = this.calls.get(block.tool_use_id);
        if (!input || block.is_error) continue;
        if (input.stop === true) {
          this.state = "stop";
          this.pendingAt = null;
        } else if (
          typeof input.delaySeconds === "number" &&
          typeof input.prompt === "string" &&
          input.prompt.trim()
        ) {
          // The delay counts from the call, not from turn end — a turn that
          // works on for ten more minutes has spent the wait already. Clamped
          // to the same [60s, 1h] the harness itself promises.
          const delay = Math.min(Math.max(input.delaySeconds, 60), 3600);
          this.state = { at: this.now() + delay * 1000, prompt: input.prompt };
          this.pendingAt = this.state.at;
        }
      }
    }
  }

  /** This turn's wake-up request, consumed. */
  take(): RunResult["wakeup"] {
    const state = this.state;
    this.state = undefined;
    return state;
  }

  /** The child's timer went off (self-wake observed); nothing pending now
   * unless the wake turn arms again. */
  fired(): void {
    this.pendingAt = null;
  }
}

/** How the parked state reads on a status receipt or in `!status`. */
export function describeTasks(tasks: BackgroundTask[]): string {
  if (!tasks.length) return "";
  if (tasks.length === 1)
    return tasks[0].description?.trim() || tasks[0].task_type || "1 task";
  return `${tasks.length} background tasks`;
}

export function isRunning(chatKey: string): boolean {
  return running.has(chatKey);
}

/** Parked on background work: the process is up but idle, with its last answer
 * already delivered. The queue treats this as free rather than busy. */
export function isWaiting(chatKey: string): boolean {
  return Boolean(running.get(chatKey)?.waiting);
}

/** A live child holds its own timer for this chat's pending wake-up. While
 * true, the gateway's timer (wakeup.ts) must defer rather than fire — the
 * child fires it in-process, with the loop's full context. */
export function hasLiveWakeup(chatKey: string): boolean {
  return running.get(chatKey)?.wakeupAt != null;
}

/** What a parked run is waiting on. Empty for anything else. */
export function waitingOn(chatKey: string): BackgroundTask[] {
  const run = running.get(chatKey);
  return run?.waiting ? run.tasks : [];
}

export function stop(chatKey: string): boolean {
  const child = running.get(chatKey)?.child;
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

/**
 * Hand a message to a run that is parked on background work, instead of
 * queueing it behind the park.
 *
 * Without this a chat that started a twenty-minute build would refuse to talk
 * until the build reported — the process is up, so the queue calls it busy,
 * when in fact it is sitting idle with an open stdin. The reply comes back
 * through the same `onTurn` the parked run was already going to use.
 *
 * False if the run is not parked (gone, or mid-turn), in which case the caller
 * should start a run of its own.
 */
export function handOff(
  chatKey: string,
  message: string,
  contextPrefix = "",
): boolean {
  const run = running.get(chatKey);
  if (!run?.waiting) return false;
  const prompt = contextPrefix ? `${contextPrefix}\n\n${message}` : message;
  if (!run.child.stdin?.writable) return false;
  leaveWait(run);
  run.child.stdin.write(userMessage(prompt));
  return true;
}

function leaveWait(run: LiveRun): void {
  run.waiting = false;
  if (run.idle) {
    clearTimeout(run.idle);
    run.idle = null;
  }
}

// Each chat gets its own mcp config so approve-mcp knows which chat to ask.
/** MCP servers registered for `cwd` at PROJECT scope in ~/.claude.json.
 *
 * Headless runs pass --strict-mcp-config so the workspace's interactive
 * config never leaks in wholesale; this is the deliberate exception. A server
 * added with `claude mcp add` from a directory is a statement that runs IN
 * that directory should have it — the trading agent's brokerage MCP being the
 * motivating case — and its stored OAuth credential matches by server name +
 * URL, so it authenticates identically here (verified against a live HTTP
 * OAuth server). Project scope only: the global mcpServers block is where
 * interactive-only servers live, and it stays out.
 */
function projectMcpServers(cwd: string): Record<string, unknown> {
  try {
    const raw = fs.readFileSync(path.join(config.home, ".claude.json"), "utf8");
    const parsed = JSON.parse(raw) as {
      projects?: Record<string, { mcpServers?: Record<string, unknown> }>;
    };
    return parsed.projects?.[path.resolve(cwd)]?.mcpServers ?? {};
  } catch {
    return {};
  }
}

function mcpConfigPath(chatKey: string, cwd?: string): string {
  const safe = chatKey.replace(/[^A-Za-z0-9]+/g, "-");
  const p = path.join(config.runtimeDir, `mcp-${safe}.json`);
  fs.mkdirSync(config.runtimeDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(
    p,
    JSON.stringify({
      mcpServers: {
        // Project servers first so the approval relay always wins the name
        // "gw" — a project config must not be able to impersonate it.
        ...(cwd ? projectMcpServers(cwd) : {}),
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
  hooks: RunHooks = {},
  run_?: RunOverrides,
): Promise<RunResult> {
  const chat: ChatState = getChat(chatKey);
  const group = isGroupChat(chatKey);
  const cwd = run_?.cwd ?? chat.cwd;
  const prompt = contextPrefix ? `${contextPrefix}\n\n${message}` : message;
  // stream-json both ways.
  //
  // Out, because the events are what drive the live status message, and the
  // init event carries session_id early enough to persist it before the run
  // can be interrupted. --verbose is required alongside it.
  //
  // In, because `-p <prompt>` closes stdin, and the CLI treats a closed stdin
  // as "that was the whole conversation": it tears down at the end of the turn
  // and KILLS any background task still running (they come back
  // `status: "killed"`). Feeding the prompt over stdin instead leaves the
  // process up, and the CLI then wakes itself when a task reports — a second
  // `result` event on the same session, which is the reply this surface used
  // to need a nudge from the phone to produce.
  const args = [
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
  ];
  args.push("--model", run_?.model ?? chat.model ?? config.model);
  args.push("--effort", run_?.effort ?? chat.effort ?? config.effort);
  // Append rather than replace: Claude Code's own system prompt is what tells
  // it which tools exist. This only adds what it can't know — that the far end
  // is a phone, not a terminal.
  const systemPrompt = group
    ? `${config.systemPrompt}\n\n${config.groups.systemPrompt}`
    : config.systemPrompt;
  if (systemPrompt) args.push("--append-system-prompt", systemPrompt);
  // A pinned run resumes its own slot's session — or nothing, when the
  // schedule wants a fresh one per firing — never the chat's live thread.
  const resumeId = run_?.session
    ? run_.fresh
      ? undefined
      : resumeIdForSlot(chat, run_.session)
    : chat.sessionId;
  if (resumeId) args.push("--resume", resumeId);
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
  } else {
    // The approval relay is wired in every 1:1 run, auto mode included.
    //
    // Auto used to be `--permission-mode bypassPermissions`, which never calls
    // the prompt tool at all — and that silently took AskUserQuestion with it.
    // A bypassed question is "allowed" with no `answers` field, so claude is
    // told the user didn't answer and carries on guessing, which is how a
    // question asked outside plan mode used to vanish. Keeping the relay wired
    // and letting approvals.ts allow ordinary tools unprompted (see
    // autoAllows there) is the same grant with the conversation left in.
    //
    // --strict-mcp-config keeps the headless run from loading the workspace's
    // interactive MCP servers; only the approval relay is wired in.
    args.push(
      "--permission-prompt-tool",
      "mcp__gw__approve",
      "--mcp-config",
      // 1:1 runs also get the MCP servers registered for their cwd at project
      // scope — see projectMcpServers. Groups stay relay-only above.
      mcpConfigPath(chatKey, cwd),
      "--strict-mcp-config",
      "--allowedTools",
      // Still worth passing under auto: a read-only tool answered here never
      // makes the socket round trip at all.
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
        cwd,
        env: { ...process.env, HOME: config.home },
        stdio: ["pipe", "pipe", "pipe"],
      });
      const run: LiveRun = {
        child,
        tasks: [],
        wakePending: false,
        turns: 0,
        waiting: false,
        idle: null,
        wakeupAt: null,
      };
      running.set(chatKey, run);

      let err = "";
      let result: RunResult | null = null;
      let sessionId: string | undefined;
      let lastAssistant = "";
      const wakeups = new WakeupTracker();
      // Turn replies go out one at a time and in order. Two of them racing on
      // the wire would put a wake-up's answer above the answer it follows.
      let deliveries: Promise<unknown> = Promise.resolve();

      // A claude that dies during startup makes the prompt write an EPIPE. It
      // is reported through the close handler like any other failed run; an
      // unhandled 'error' on the stream would take the whole gateway down.
      child.stdin!.on("error", (e) => {
        err += `stdin: ${(e as Error).message}\n`;
      });
      child.stdin!.write(userMessage(prompt));

      /** Stop feeding the process, which is how it is asked to exit. */
      const endInput = (): void => {
        leaveWait(run);
        if (child.stdin?.writable) child.stdin.end();
      };

      /** Park until the CLI wakes itself, or until we give up on it. */
      const enterWait = (): void => {
        run.waiting = true;
        run.idle = setTimeout(() => {
          const stranded = run.tasks;
          endInput();
          if (stranded.length) hooks.onAbandon?.(stranded, "timeout");
        }, config.background.waitMs);
        // A parked run must never be the reason the gateway won't exit.
        run.idle.unref?.();
      };

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
          // A pinned run's id goes to its slot; the chat's live pointer is
          // someone else's thread. Re-fetch the chat: the user may have
          // switched sessions since this run started.
          if (run_?.session)
            updateChat(
              chatKey,
              sessionPatchForSlot(getChat(chatKey), run_.session, sessionId),
            );
          else updateChat(chatKey, { sessionId });
        }
        if (ev.type === "system") {
          // The whole outstanding list, every time — assign, don't merge.
          if (ev.subtype === "background_tasks_changed") run.tasks = ev.tasks ?? [];
          else if (ev.subtype === "task_notification") run.wakePending = true;
          else if (ev.subtype === "init") {
            // A turn is starting. On a wake-up this is the moment the park
            // ends, and the notification that caused it is now spoken for.
            //
            // A turn that starts while parked with NO task notification
            // pending and no handOff (that would have left the wait already)
            // has exactly one other cause: the child's own ScheduleWakeup
            // timer went off. The gateway's persisted copy is spent.
            if (run.turns > 0 && run.waiting && !run.wakePending && run.wakeupAt !== null) {
              wakeups.fired();
              hooks.onSelfWake?.();
            }
            run.wakePending = false;
            if (run.turns > 0) leaveWait(run);
          }
        }
        const spoke = assistantText(ev);
        if (spoke) lastAssistant = spoke;
        wakeups.onEvent(ev);
        run.wakeupAt = wakeups.pendingAt;
        if (ev.type === "result") {
          result = {
            text: pickResultText(ev.result, lastAssistant),
            sessionId: ev.session_id ?? sessionId,
            isError: Boolean(ev.is_error),
            wakeup: wakeups.take(),
          };
          // The next turn writes its own words; carrying these over would let
          // a wake-up with no text repeat the previous turn's answer.
          lastAssistant = "";
          run.turns++;
          const turn = run.turns;
          const delivered = result;
          const pending = run.tasks;
          deliveries = deliveries.then(() =>
            hooks.onTurn?.(delivered, turn, pending),
          );

          // Is there more coming? `tasks` is the CLI's own outstanding list;
          // `wakePending` covers the gap where it has already cleared the list
          // but not yet started the turn it cleared it for.
          const more = pending.length > 0 || run.wakePending;
          if (!more || !config.background.waitMs) endInput();
          else if (turn >= config.background.maxTurns) {
            endInput();
            if (pending.length) hooks.onAbandon?.(pending, "turns");
          } else enterWait();
        }
        try {
          hooks.onEvent?.(ev);
        } catch (e) {
          // A broken progress renderer must not take the run down with it.
          console.warn(`onEvent failed: ${(e as Error).message}`);
        }
      };

      // Off unless configured (config.claudeTimeoutMs). A run has no idea how
      // long it should take, and the only thing a wall clock reliably kills is
      // the long job that was working.
      const timer = config.claudeTimeoutMs
        ? setTimeout(() => {
            child.kill("SIGTERM");
            err += `\n(timed out after ${config.claudeTimeoutMs / 60000}m)`;
          }, config.claudeTimeoutMs)
        : null;

      child.on("close", (code) => {
        if (timer) clearTimeout(timer);
        leaveWait(run);
        running.delete(chatKey);
        if (buf.trim()) handleLine(buf);
        // No result event means the run died before finishing — killed by
        // !stop, the timeout, or a SIGTERM from a redeploy. Say what happened
        // rather than hand back an empty answer.
        const final: RunResult = result ?? {
          text:
            `claude exited ${code}` +
            (err.trim() ? `\n${err.trim().slice(-800)}` : ""),
          sessionId,
          isError: true,
          aborted: true,
        };
        // Not before the last turn's reply has actually gone out: the caller
        // takes this as "the run is over" and starts draining the queue.
        void deliveries.then(() => resolve(final));
      });
    });
  } finally {
    activeCount--;
    updateChat(chatKey, { inFlight: false });
  }
}

/** Bytes of transcript a cold resume would have to rebuild into context: the
 * session jsonl's tail after its last compact boundary. The file itself only
 * ever grows — compaction appends a boundary rather than rewriting — so raw
 * size overstates a compacted thread; the tail is what has to fit back into
 * the window. 0 when the transcript doesn't exist yet. */
export function resumableBytes(cwd: string, sessionId: string): number {
  const p = path.join(
    config.home,
    ".claude/projects",
    cwd.replace(/[/.]/g, "-"),
    `${sessionId}.jsonl`,
  );
  try {
    const text = fs.readFileSync(p, "utf8");
    const boundary = text.lastIndexOf('"compact_boundary"');
    return boundary < 0 ? text.length : text.length - boundary;
  } catch {
    return 0;
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
