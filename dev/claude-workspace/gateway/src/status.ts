import { config } from "./config.ts";
import type { StreamEvent } from "./claude.ts";
import {
  editMsg,
  canEdit,
  editIntervalFor,
  sendOne,
  type MsgRef,
} from "./transport.ts";

// The live status message: one message per run, sent when the run starts and
// edited in place as tool calls land, then collapsed to a single line when the
// answer goes out.
//
// It is deliberately NOT the reply. sendTo() fans one logical answer into up to
// four chunks, so "the message I just sent" isn't a single message and can't be
// an edit target — and WhatsApp refuses edits past ~15 minutes, which a long
// run would blow through. Keeping the two separate means the answer is always
// an ordinary, un-truncated, un-edited message.

/** Text shown while nothing has happened yet. */
const INITIAL = "⏺ working…";

export interface StatusIO {
  send(chatKey: string, text: string): Promise<MsgRef | undefined>;
  edit(chatKey: string, ref: MsgRef, text: string): Promise<boolean>;
  supported(chatKey: string): boolean;
}

const defaultIO: StatusIO = {
  send: sendOne,
  edit: editMsg,
  supported: canEdit,
};

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

/** The tail of the receipt: what the run cost. Both halves are optional —
 * a run killed before its result event reports neither, and a subscription
 * doesn't always price a run, so an absent cost is normal rather than zero. */
export function formatSpend(tokens: number, costUsd: number): string {
  let out = "";
  if (tokens > 0)
    out += ` · ${tokens >= 1000 ? `${Math.round(tokens / 1000)}k` : tokens} tok`;
  if (costUsd > 0)
    out += ` · $${costUsd < 0.01 ? costUsd.toFixed(3) : costUsd.toFixed(2)}`;
  return out;
}

/** Shorten a path to its last two segments — `src/router.ts` reads fine on a
 * phone, `/home/node/code/selfhosted/dev/.../src/router.ts` does not. */
function shortPath(p: unknown): string {
  const parts = String(p ?? "").split("/").filter(Boolean);
  return parts.slice(-2).join("/") || String(p ?? "");
}

function oneLine(s: unknown, max = 60): string {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

/** A human label for a tool_use block. Unknown tools fall back to their name
 * rather than being hidden: a surprise in the status line is information. */
export function toolLabel(name: string, input: Record<string, unknown>): string {
  // MCP tools arrive as mcp__server__tool; the last segment is the useful part.
  const short = name.includes("__") ? name.split("__").pop()! : name;
  switch (short) {
    case "Read":
    case "Write":
    case "NotebookEdit":
      return `${short} ${shortPath(input.file_path ?? input.notebook_path)}`;
    case "Edit":
      return `Edit ${shortPath(input.file_path)}`;
    case "Bash":
      return `Bash: ${oneLine(input.command)}`;
    case "Grep":
      return `Grep ${oneLine(input.pattern, 40)}`;
    case "Glob":
      return `Glob ${oneLine(input.pattern, 40)}`;
    case "WebFetch":
      return `Fetch ${oneLine(hostOf(input.url), 40)}`;
    case "WebSearch":
      return `Search ${oneLine(input.query, 40)}`;
    case "Task":
    case "Agent":
      return `Agent: ${oneLine(input.description ?? input.prompt, 40)}`;
    case "TodoWrite":
      return "planning";
    case "approve":
      // The approval relay — the run is blocked on a reply from this chat, and
      // saying so is the difference between "slow" and "waiting on you".
      return "waiting for your approval";
    default:
      return short;
  }
}

function hostOf(url: unknown): string {
  try {
    return new URL(String(url)).host;
  } catch {
    return String(url ?? "");
  }
}

interface Snapshot {
  action: string;
  note: string;
  tools: number;
  elapsedMs: number;
}

export function renderStatus(s: Snapshot): string {
  const head = `⏺ working… ${formatElapsed(s.elapsedMs)}${s.tools ? ` · ${s.tools} tool${s.tools === 1 ? "" : "s"}` : ""}`;
  return [head, s.action, s.note].filter(Boolean).join("\n");
}

export class Status {
  private ref: MsgRef | undefined;
  private started = 0;
  private tools = 0;
  private action = "";
  private note = "";
  private tokens = 0;
  private costUsd = 0;
  private lastText = "";
  private timer: ReturnType<typeof setInterval> | null = null;
  /** True while an edit is out on the wire. A tick that arrives mid-edit is
   * dropped rather than queued: on a slow link the queue would grow forever
   * and the status would fall further behind the run with every tick. */
  private editing = false;
  /** Edits are chained rather than fired in parallel: two in flight at once can
   * land out of order, leaving the status showing a step the run already
   * finished. */
  private chain: Promise<unknown> = Promise.resolve();
  private done = false;

  constructor(
    private chatKey: string,
    private io: StatusIO = defaultIO,
    /** Overridable so tests can run the throttle at millisecond scale rather
     * than waiting out the real one. */
    private intervalMs: number = editIntervalFor(chatKey),
    /** Also test-only: a fake clock lets the tick behaviour be checked in
     * milliseconds instead of the real seconds the display counts in. */
    private now: () => number = Date.now,
  ) {}

  /** Send the initial message and start the clock. No-op if progress is off or
   * the surface can't edit — a status message that never updates is worse than
   * none. */
  async begin(): Promise<void> {
    this.started = this.now();
    if (!config.progress.enabled || !this.io.supported(this.chatKey)) return;
    this.ref = await this.io.send(this.chatKey, INITIAL);
    this.lastText = INITIAL;
    // A free-running ticker, not a timer armed by events. Events are exactly
    // what a long tool call stops producing, and that silence used to freeze
    // the status mid-run; on a ticker the elapsed clock keeps counting and the
    // message stays visibly alive whatever claude is busy with.
    this.timer = setInterval(() => void this.flush(), this.intervalMs);
    // Bun/Node keep the process alive for a pending timer; a status message
    // must never be the reason the gateway won't exit.
    this.timer.unref?.();
  }

  get active(): boolean {
    return this.ref !== undefined && !this.done;
  }

  onEvent(ev: StreamEvent): void {
    if (ev.type === "result") {
      // Everything the run cost, including cache traffic — the point is "was
      // that an expensive question?", which cache reads very much are part of.
      const u = ev.usage ?? {};
      this.tokens =
        (u.input_tokens ?? 0) +
        (u.output_tokens ?? 0) +
        (u.cache_creation_input_tokens ?? 0) +
        (u.cache_read_input_tokens ?? 0);
      this.costUsd = ev.total_cost_usd ?? 0;
      return;
    }
    if (ev.type !== "assistant") return;
    for (const block of ev.message?.content ?? []) {
      if (block.type === "tool_use") {
        this.tools++;
        this.action = toolLabel(block.name ?? "?", block.input ?? {});
      } else if (block.type === "text") {
        const text = (block.text ?? "").trim();
        // Only the first line: mid-turn narration can be a paragraph, and the
        // status message is a status, not a second copy of the answer.
        if (text) this.note = oneLine(text.split("\n")[0], 140);
      }
    }
  }

  /** One redraw, at most one edit in flight. The rendered text is what gets
   * compared: the elapsed clock is part of it, so a run that is merely still
   * running does redraw — that is the point — while a tick that would produce
   * byte-identical text (sub-second intervals, a stopped clock) does not. */
  private flush(): Promise<unknown> {
    if (!this.active || this.editing) return this.chain;
    const text = renderStatus({
      action: this.action,
      note: this.note,
      tools: this.tools,
      elapsedMs: this.now() - this.started,
    });
    if (text === this.lastText) return this.chain;
    this.lastText = text;
    this.editing = true;
    this.chain = this.chain
      .then(() => this.io.edit(this.chatKey, this.ref, text))
      .finally(() => {
        this.editing = false;
      });
    return this.chain;
  }

  /** Collapse to one line. The run is over; what stays in the thread is a
   * receipt, not a stale half-finished action. */
  async finish(ok: boolean): Promise<void> {
    await this.replace(
      `${ok ? "✓" : "⚠"} ${ok ? "done" : "failed"}` +
        ` · ${this.tools} tool${this.tools === 1 ? "" : "s"}` +
        ` · ${formatElapsed(this.now() - this.started)}` +
        formatSpend(this.tokens, this.costUsd),
    );
  }

  /** Overwrite with an arbitrary line and stop updating (SIGTERM path). */
  async replace(text: string): Promise<void> {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (!this.active) {
      this.done = true;
      return;
    }
    const ref = this.ref;
    this.done = true;
    this.lastText = text;
    this.chain = this.chain.then(() => this.io.edit(this.chatKey, ref, text));
    await this.chain;
  }
}

export function createStatus(chatKey: string, io?: StatusIO): Status {
  return new Status(chatKey, io);
}
