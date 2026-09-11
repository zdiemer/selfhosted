import { config, verbosityMs } from "./config.ts";
import type { StreamEvent } from "./claude.ts";
import { getChat } from "./state.ts";
import {
  editMsg,
  canEdit,
  minEditIntervalFor,
  sendOne,
  type EditResult,
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
/** What a retired status message is left saying, by why it was retired. */
const CONTINUED = "⏺ continued below";
const ANSWERED = "⏺ answered — continuing below";

/** How many edits one message is worth. Signal caps a message at ten revisions
 * (MessageConstraintsUtil.MAX_EDIT_COUNT in Signal-Android) and silently drops
 * the rest, so this is a hard budget, not a preference: the last one is
 * reserved for the line the message ends on — a receipt, or a pointer at the
 * message the status rolled onto — leaving nine for progress. */
const MAX_EDITS = 10;
const RECEIPT_EDITS = 1;

export interface StatusIO {
  send(chatKey: string, text: string): Promise<MsgRef | undefined>;
  edit(chatKey: string, ref: MsgRef, text: string): Promise<EditResult>;
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

/**
 * Redraw cadence for a run in this chat: what its verbosity asks for, never
 * faster than the surface allows. Zero means no status message at all — a
 * `quiet` chat, or a surface whose floor is the way it says "not here".
 *
 * Read per run rather than held, so `!verbose` takes effect on the next
 * message instead of the next restart.
 */
export function statusIntervalFor(chatKey: string): number {
  const want = verbosityMs(getChat(chatKey).verbosity);
  return want <= 0 ? 0 : Math.max(want, minEditIntervalFor(chatKey));
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
  /** Progress edits spent so far, against the budget. */
  private spent = 0;
  private lastEditAt = 0;
  /** Edits are chained rather than fired in parallel: two in flight at once can
   * land out of order, leaving the status showing a step the run already
   * finished. */
  private chain: Promise<unknown> = Promise.resolve();
  private done = false;
  /** Bumped by a roll. An edit that was already on the wire when the status
   * moved to a new message must not write its `next` ref back over it. */
  private epoch = 0;
  /** Status messages this run has used so far. Sets the cadence (see gap) and
   * is what `progress.maxMessages` is spent against. */
  private rolls = 0;
  /** The move to a fresh message, while it is in flight. A receipt that lands
   * mid-roll has to wait for it, or it writes itself onto the message the roll
   * just retired. */
  private rolling: Promise<void> | null = null;

  constructor(
    private chatKey: string,
    private io: StatusIO = defaultIO,
    /** Overridable so tests can run the throttle at millisecond scale rather
     * than waiting out the real one. Zero or less is `quiet`: no status
     * message for this run. */
    private intervalMs: number = statusIntervalFor(chatKey),
    /** Also test-only: a fake clock lets the tick behaviour be checked in
     * milliseconds instead of the real seconds the display counts in. */
    private now: () => number = Date.now,
  ) {}

  /** Send the initial message and start the clock. No-op if progress is off,
   * the chat asked for none (`!verbose quiet`), or the surface can't edit — a
   * status message that never updates is worse than none.
   *
   * The clock still starts: `finish` reports the run's elapsed time either way,
   * and on a quiet chat that receipt is simply never sent. */
  async begin(): Promise<void> {
    this.started = this.now();
    if (
      !config.progress.enabled ||
      this.intervalMs <= 0 ||
      !this.io.supported(this.chatKey)
    )
      return;
    this.ref = await this.io.send(this.chatKey, INITIAL);
    this.lastText = INITIAL;
    this.lastEditAt = this.now();
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

  /**
   * How long to wait before spending the next progress edit.
   *
   * Steady inside one message. The gap used to double per EDIT, which spread
   * nine revisions over three quarters of an hour — dense for the first ten
   * seconds and then, for most of a long run, a message showing a tool call
   * from four minutes ago beside a clock that had stopped.
   *
   * What doubles now is the cadence of each successive MESSAGE. Nine edits at
   * the chat's verbosity cover the start of the run step by step, and when
   * that budget runs out the status rolls onto a fresh message at twice the
   * step. A long run costs a handful of messages instead of eighty, and none
   * of them is ever more than one cadence stale.
   */
  private gap(): number {
    return this.intervalMs * 2 ** this.rolls;
  }

  /** One redraw, at most one edit in flight. The rendered text is what gets
   * compared: the elapsed clock is part of it, so a run that is merely still
   * running does redraw — that is the point — while a tick that would produce
   * byte-identical text (sub-second intervals, a stopped clock) does not. */
  private flush(): Promise<unknown> {
    if (!this.active || this.editing) return this.chain;
    if (this.spent >= MAX_EDITS - RECEIPT_EDITS) {
      // Out of revisions on this message. Signal drops the rest silently, so
      // the choice is a new message or no more progress at all — and going
      // quiet halfway through is what made a long run look hung.
      if (this.rolls < config.progress.maxMessages - 1)
        void this.rollOnce(CONTINUED);
      return this.chain;
    }
    if (this.now() - this.lastEditAt < this.gap()) return this.chain;
    const text = renderStatus({
      action: this.action,
      note: this.note,
      tools: this.tools,
      elapsedMs: this.now() - this.started,
    });
    if (text === this.lastText) return this.chain;
    this.lastText = text;
    this.editing = true;
    this.spent++;
    this.lastEditAt = this.now();
    const epoch = this.epoch;
    const ref = this.ref;
    this.chain = this.chain
      .then(() => this.io.edit(this.chatKey, ref, text))
      // Signal makes every revision its own message and only the newest can be
      // edited again, so the target has to move with it or the rest of the run
      // is written to a message the clients have stopped listening to.
      .then((res) => {
        if (epoch === this.epoch && res.next !== undefined) this.ref = res.next;
      })
      .finally(() => {
        this.editing = false;
      });
    return this.chain;
  }

  /**
   * Move the status onto a fresh message and keep counting.
   *
   * Two things ask for this. An answered prompt: a run that stops for a plan,
   * a question or a permission ask spends the wait on screen, and by the time
   * the reply lands the old message is stranded — its revisions gone, the
   * prompt and the reply the newest things in the thread, the status scrolled
   * somewhere above them. And a spent edit budget: nine revisions is all
   * Signal allows one message, and a run longer than that has to continue
   * somewhere.
   *
   * The elapsed clock, the tool count and the roll count all carry over — it
   * is the same run, and the next message is paced accordingly.
   */
  private async roll(retire: string): Promise<void> {
    this.epoch++;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    const old = this.ref;
    // Not active while this is in flight, which is what keeps the ticker and a
    // concurrent flush off the message being retired.
    this.ref = undefined;
    this.rolls++;
    // Retire the old message rather than leave a "working…" that will never
    // move again next to one that does. This is what RECEIPT_EDITS reserved.
    this.chain = this.chain.then(() => this.io.edit(this.chatKey, old, retire));
    const ref = await this.io.send(this.chatKey, INITIAL);
    if (ref === undefined) return; // send failed; nothing left to edit
    this.ref = ref;
    this.lastText = INITIAL;
    this.spent = 0;
    this.lastEditAt = this.now();
    this.timer = setInterval(() => void this.flush(), this.intervalMs);
    this.timer.unref?.();
  }

  /** One roll at a time. The ticker keeps firing while a roll is on the wire,
   * and a second one would strand the message the first had just sent. */
  private rollOnce(retire: string): Promise<void> {
    if (this.rolling) return this.rolling;
    const p = this.roll(retire).finally(() => (this.rolling = null));
    this.rolling = p;
    return p;
  }

  /** The prompt was answered — hand the status to a message below the reply. */
  async restart(): Promise<void> {
    if (!this.active) return;
    // Whatever it was doing, it was waiting on the reply; that is finished now.
    this.action = "";
    this.note = "";
    await this.rollOnce(ANSWERED);
  }

  /** Collapse to one line. The turn is over; what stays in the thread is a
   * receipt, not a stale half-finished action.
   *
   * `note` is appended when the turn is not the last word — a run parked on
   * background work has answered, but is still holding, and the receipt is the
   * only place that shows the difference between "finished" and "waiting on
   * the build". */
  async finish(ok: boolean, note = ""): Promise<void> {
    await this.replace(
      `${ok ? "✓" : "⚠"} ${ok ? "done" : "failed"}` +
        ` · ${this.tools} tool${this.tools === 1 ? "" : "s"}` +
        ` · ${formatElapsed(this.now() - this.started)}` +
        formatSpend(this.tokens, this.costUsd) +
        (note ? ` · ${note}` : ""),
    );
  }

  /** Overwrite with an arbitrary line and stop updating (SIGTERM path). */
  async replace(text: string): Promise<void> {
    // A roll can be in flight — the budget ran out just as the turn ended. The
    // receipt belongs on the message that roll is sending, not on the one it
    // is retiring, and mid-roll there is no `ref` to write to at all.
    if (this.rolling) await this.rolling;
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
