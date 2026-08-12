import { config } from "./config.ts";
import type { StreamEvent } from "./claude.ts";
import { editMsg, canEdit, sendOne, type MsgRef } from "./transport.ts";

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
  private lastText = "";
  private lastSig = "";
  private lastEditAt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
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
    private intervalMs: number = config.progress.editIntervalMs,
  ) {}

  private now(): number {
    return Date.now();
  }

  private signature(): string {
    return `${this.tools}|${this.action}|${this.note}`;
  }

  /** Send the initial message. No-op if progress is off or the surface can't
   * edit — a status message that never updates is worse than none. */
  async begin(): Promise<void> {
    this.started = this.now();
    if (!config.progress.enabled || !this.io.supported(this.chatKey)) return;
    this.ref = await this.io.send(this.chatKey, INITIAL);
    this.lastText = INITIAL;
    // The message just sent already IS the empty state, so an early event that
    // carries nothing new (a blank text block, say) must not redraw it.
    this.lastSig = this.signature();
    this.lastEditAt = this.now();
  }

  get active(): boolean {
    return this.ref !== undefined && !this.done;
  }

  onEvent(ev: StreamEvent): void {
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
    this.schedule();
  }

  /** Rate-limited redraw. Baileys is an unofficial WhatsApp client and the
   * chart README is explicit about ban risk, so an edit per event is not an
   * option; the trailing timer guarantees the last state still lands. */
  private schedule(): void {
    if (!this.active || this.timer) return;
    const wait = Math.max(0, this.intervalMs - (this.now() - this.lastEditAt));
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, wait);
  }

  private flush(): Promise<unknown> {
    if (!this.active) return this.chain;
    // Compare the *content*, not the rendered text — the elapsed clock changes
    // on every event, so comparing rendered text would spend an edit on a run
    // that has not actually done anything new. Empty text blocks between tool
    // calls are common enough for this to matter.
    const sig = this.signature();
    if (sig === this.lastSig) return this.chain;
    this.lastSig = sig;
    const text = renderStatus({
      action: this.action,
      note: this.note,
      tools: this.tools,
      elapsedMs: this.now() - this.started,
    });
    this.lastText = text;
    this.lastEditAt = this.now();
    this.chain = this.chain.then(() =>
      this.io.edit(this.chatKey, this.ref, text),
    );
    return this.chain;
  }

  /** Collapse to one line. The run is over; what stays in the thread is a
   * receipt, not a stale half-finished action. */
  async finish(ok: boolean): Promise<void> {
    await this.replace(
      `${ok ? "✓" : "⚠"} ${ok ? "done" : "failed"} · ${this.tools} tool${this.tools === 1 ? "" : "s"} · ${formatElapsed(this.now() - this.started)}`,
    );
  }

  /** Overwrite with an arbitrary line and stop updating (SIGTERM path). */
  async replace(text: string): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
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
