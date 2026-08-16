import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { isGroupChat } from "./chat.ts";
import { approvalSocketPath, config } from "./config.ts";
import { autoActive, getChat, updateChat } from "./state.ts";
import type { MsgRef } from "./transport.ts";

// The approve-mcp stdio server (grandchild of this process, via claude) dials
// this unix socket with {chatKey, toolName, input} and blocks until the human
// answers over chat (or the timeout denies). One pending approval per chat is
// enough: claude runs are serialized per chat, and claude itself awaits the
// permission tool before continuing.
//
// Two tools arrive here that are not really permission requests at all —
// AskUserQuestion and ExitPlanMode are how claude talks to the human, and the
// generic "Claude wants: Tool({json…})" prompt renders them as a truncated
// blob with no way to actually answer. They get their own prompt and their own
// reply grammar below; everything else keeps the plain 1/2/3 flow.

/** A question from AskUserQuestion, as claude's schema shapes it. */
interface Question {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options?: { label: string; description?: string }[];
}

export interface PendingApproval {
  chatKey: string;
  toolName: string;
  input: unknown;
  resolve: (verdict: Verdict) => void;
  /** Which reply grammar applies. */
  kind: PromptKind;
  /** Questions still unanswered, head first (kind === "question"). */
  queue?: Question[];
  /** Answers collected so far, keyed by question text (kind === "question"). */
  answers?: Record<string, string>;
  /** How many questions there were to begin with, for the "(2/3)" counter. */
  total?: number;
  /** The prompt message itself, so a reaction ON IT can answer it. Re-recorded
   * for each question of a multi-question ask, so a 👍 always answers the
   * question actually on screen. */
  promptRef?: MsgRef;
}

export type PromptKind = "tool" | "question" | "plan";

export type Verdict =
  | { behavior: "allow"; updatedInput: unknown }
  | { behavior: "deny"; message: string };

const pending = new Map<string, PendingApproval>();

// Tool names the user answered "3 = allow all like this" for, per session-ish
// lifetime (in-memory; resets on pod restart, which is the safe direction).
const autoApproved = new Map<string, Set<string>>();

export type ApprovalPrompt = (
  chatKey: string,
  text: string,
) => Promise<MsgRef | undefined> | void;

// Set once the server starts. A multi-question AskUserQuestion has to send the
// next question from inside answerPending(), which the router calls.
let prompt: ApprovalPrompt = () => {};
let server: net.Server | null = null;

/** Send a prompt and remember which message it is, so a reaction on that
 * message can answer it. Fire-and-forget: the send is async, the caller is
 * not, and a lost ref only costs the reaction shortcut. */
function ask(chatKey: string, text: string): void {
  void (async () => {
    const ref = await prompt(chatKey, text);
    const p = pending.get(chatKey);
    if (p && ref !== undefined) p.promptRef = ref;
  })();
}

/** The message the open prompt was sent as, for matching an inbound reaction. */
export function pendingPromptRef(chatKey: string): MsgRef | undefined {
  return pending.get(chatKey)?.promptRef;
}

export function hasPending(chatKey: string): boolean {
  return pending.has(chatKey);
}

/** Which grammar the open prompt takes, for a caller that wants to treat a
 * conversational answer (question, plan) differently from a 1/2/3 on a tool. */
export function pendingKind(chatKey: string): PromptKind | undefined {
  return pending.get(chatKey)?.kind;
}

/** Route a chat reply to the waiting approval. Returns false if the reply
 * wasn't an answer to anything — the router then treats it as a new message.
 *
 * Plain tool prompts take 1/2/3 only, so an unrelated message typed while one
 * is open still reaches claude. Questions and plans also accept free text,
 * because "none of those, do X" is a real answer to both and there is nowhere
 * else for it to go. */
export function answerPending(chatKey: string, reply: string): boolean {
  const p = pending.get(chatKey);
  if (!p) return false;
  const answer = reply.trim();
  if (!answer) return false;

  if (p.kind === "question") return answerQuestion(chatKey, p, answer);
  if (p.kind === "plan") return answerPlan(chatKey, p, answer);

  if (!["1", "2", "3"].includes(answer)) return false;
  pending.delete(chatKey);
  if (answer === "2") {
    p.resolve({ behavior: "deny", message: "denied by user over chat" });
  } else {
    if (answer === "3") {
      let set = autoApproved.get(chatKey);
      if (!set) autoApproved.set(chatKey, (set = new Set()));
      set.add(p.toolName);
    }
    p.resolve({ behavior: "allow", updatedInput: p.input });
  }
  return true;
}

// ---------------------------------------------------------------------------
// AskUserQuestion
// ---------------------------------------------------------------------------

/** Options as a numbered list the user can answer with digits. Descriptions
 * are trimmed hard — the question and the labels are what a phone needs. */
export function renderQuestion(q: Question, index: number, total: number): string {
  const lines: string[] = [];
  const counter = total > 1 ? ` (${index + 1}/${total})` : "";
  lines.push(`❓ ${q.header ? `${q.header}: ` : ""}${q.question}${counter}`);
  (q.options ?? []).forEach((o, i) => {
    const desc = o.description ? ` — ${truncate(o.description, 100)}` : "";
    lines.push(`${i + 1}. ${o.label}${desc}`);
  });
  lines.push(
    q.multiSelect
      ? "Reply with numbers (e.g. 1,3), or type your own answer."
      : "Reply with a number, or type your own answer.",
  );
  return lines.join("\n");
}

/** Map a reply onto option labels. Digits select; anything else is the user's
 * own answer, which is exactly what the tool's "Other" choice means. */
export function selectOptions(reply: string, q: Question): string {
  const options = q.options ?? [];
  const picks = reply.split(/[,\s]+/).filter(Boolean);
  const indexes = picks.map((p) => Number(p));
  const allDigits =
    picks.length > 0 &&
    indexes.every((n) => Number.isInteger(n) && n >= 1 && n <= options.length);
  if (!allDigits) return reply;
  const chosen = (q.multiSelect ? indexes : indexes.slice(0, 1)).map(
    (n) => options[n - 1].label,
  );
  return [...new Set(chosen)].join(", ");
}

function answerQuestion(
  chatKey: string,
  p: PendingApproval,
  reply: string,
): boolean {
  const queue = p.queue ?? [];
  const q = queue[0];
  if (!q) {
    pending.delete(chatKey);
    p.resolve({ behavior: "allow", updatedInput: p.input });
    return true;
  }

  const answers = { ...(p.answers ?? {}), [q.question]: selectOptions(reply, q) };
  const rest = queue.slice(1);
  if (rest.length) {
    // More to ask: keep the approval open and send the next one.
    pending.set(chatKey, { ...p, queue: rest, answers });
    const total = p.total ?? queue.length;
    ask(chatKey, renderQuestion(rest[0], total - rest.length, total));
    return true;
  }

  pending.delete(chatKey);
  // `answers` is the channel claude's own permission component uses to hand
  // back what the user picked; without it the tool reports "did not answer".
  p.resolve({
    behavior: "allow",
    updatedInput: { ...(p.input as object), answers },
  });
  // Say the answer back. Between a reply and whatever claude does with it there
  // can be minutes of silence, and without this the last thing on screen is the
  // question — indistinguishable from a reply that was never received.
  prompt(chatKey, `✓ answered: ${truncate(answers[q.question] || reply, 120)}`);
  return true;
}

// ---------------------------------------------------------------------------
// ExitPlanMode
// ---------------------------------------------------------------------------

/** The plan itself is the thing worth reading, so send it rather than a
 * truncated JSON blob. Markdown is de-marked because this surface renders none
 * of it. */
export function renderPlan(planText: string): string {
  return (
    `📋 Plan:\n\n${plainText(planText)}\n\n` +
    "Reply 1 approve · 2 keep planning · or say what to change"
  );
}

function answerPlan(chatKey: string, p: PendingApproval, reply: string): boolean {
  pending.delete(chatKey);
  if (reply === "1") {
    // Approving a plan means "go do it". Leaving plan mode on would send the
    // very next message back into planning, which reads as the approval having
    // been ignored.
    updateChat(chatKey, { plan: false });
    p.resolve({ behavior: "allow", updatedInput: p.input });
    prompt(chatKey, "✓ approved — plan mode off, going ahead");
    return true;
  }
  // Anything else is feedback. Handing it back as the denial message puts it in
  // front of claude as the reason, so "2 but skip the tests" keeps planning
  // with the note attached instead of being thrown away.
  const note = reply === "2" ? "" : `: ${reply}`;
  p.resolve({
    behavior: "deny",
    message: `user wants to keep planning${note}`,
  });
  // Same reason as a question's ack: replanning is not quick, and an
  // unacknowledged "2, but skip the tests" looks exactly like a lost message.
  prompt(
    chatKey,
    reply === "2"
      ? "✎ still planning — revising"
      : `✎ still planning — taking: ${truncate(reply, 120)}`,
  );
  return true;
}

// ---------------------------------------------------------------------------

function describeTool(toolName: string, input: unknown): string {
  const i = input as Record<string, unknown> | null;
  if (toolName === "Bash" && i?.command) return `Bash(${i.command})`;
  if (i?.file_path) return `${toolName}(${i.file_path})`;
  const json = JSON.stringify(i ?? {});
  return `${toolName}(${json.length > 200 ? json.slice(0, 200) + "…" : json})`;
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Strip the markdown this surface can't render (headings, emphasis, code
 * fences, bullet dashes) so a plan reads as plain lines on a phone. */
export function plainText(s: string): string {
  return s
    .replace(/^```.*$/gm, "")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/^\s*[-*]\s+/gm, "• ")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Does `!auto` cover this chat right now? Auto is a standing "don't ask
 * before touching things", so an ordinary tool is allowed here without a trip
 * to the phone — the same grant bypassPermissions used to give, minus its
 * silent swallowing of AskUserQuestion. Plan mode outranks it, as everywhere
 * else. */
function autoAllows(chatKey: string): boolean {
  const chat = getChat(chatKey);
  return autoActive(chat) && !chat.plan;
}

/** Which reply grammar a tool gets. */
export function promptKind(toolName: string): PromptKind {
  if (toolName === "AskUserQuestion") return "question";
  if (toolName === "ExitPlanMode") return "plan";
  return "tool";
}

export async function startApprovalServer(
  sendPrompt: ApprovalPrompt,
): Promise<void> {
  prompt = sendPrompt;
  fs.mkdirSync(path.dirname(approvalSocketPath), {
    recursive: true,
    mode: 0o700,
  });
  // Unlinking a LIVE socket is silent and vicious: the running gateway keeps
  // the unlinked inode and notices nothing, while every approve-mcp from then
  // on gets ENOENT on the path and the in-flight claude run is stranded with no
  // way to ask for permission. That is exactly what a second copy started for a
  // smoke test does, so check before removing — a stale socket (nobody
  // listening) is the only one safe to clear.
  if (fs.existsSync(approvalSocketPath) && (await socketAnswers())) {
    console.error(
      `another gateway is listening on ${approvalSocketPath}; refusing to ` +
        "clobber it. Set GW_RUNTIME_DIR to a scratch path to run a second copy.",
    );
    process.exit(1);
  }
  fs.rmSync(approvalSocketPath, { force: true });

  server = net.createServer((sock) => {
    let buf = "";
    sock.on("data", (d) => {
      buf += d.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      let req: { chatKey: string; toolName: string; input: unknown };
      try {
        req = JSON.parse(buf.slice(0, nl));
      } catch {
        sock.end();
        return;
      }
      handleRequest(req, sock, sendPrompt);
    });
    sock.on("error", () => {});
  });
  server.listen(approvalSocketPath);
}

/** Close the listening socket. Only tests need this — the gateway itself keeps
 * it open for the life of the process. */
export function stopApprovalServer(): void {
  server?.close();
  server = null;
}

/** True if something is accepting connections on the approval socket path. */
function socketAnswers(): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = net.connect(approvalSocketPath);
    const done = (answered: boolean) => {
      probe.destroy();
      resolve(answered);
    };
    probe.on("connect", () => done(true));
    probe.on("error", () => done(false));
    setTimeout(() => done(false), 1000);
  });
}

function handleRequest(
  req: { chatKey: string; toolName: string; input: unknown },
  sock: net.Socket,
  sendPrompt: ApprovalPrompt,
): void {
  const finish = (verdict: Verdict) => {
    sock.write(JSON.stringify(verdict) + "\n");
    sock.end();
  };

  // Groups are never asked. Anything outside groups.allowedTools is refused
  // here rather than relayed: the room would see the prompt, only one member
  // could answer it, and a "3 = allow all" from that member would quietly widen
  // what everyone else can reach for the rest of the session.
  if (isGroupChat(req.chatKey)) {
    finish({
      behavior: "deny",
      message:
        `${req.toolName} is not available in group chats. ` +
        "Answer from the conversation, or use WebFetch/WebSearch.",
    });
    return;
  }

  const kind = promptKind(req.toolName);

  // "3 = allow all" and auto mode are both meaningless for these two and
  // actively harmful: a blind allow of AskUserQuestion returns no answers, so
  // the tool reports that the user didn't answer — which is how a question can
  // silently vanish. Questions and plans are conversation; they always ask.
  if (
    kind === "tool" &&
    (autoAllows(req.chatKey) || autoApproved.get(req.chatKey)?.has(req.toolName))
  ) {
    finish({ behavior: "allow", updatedInput: req.input });
    return;
  }

  // A second concurrent ask for the same chat shouldn't happen (runs are
  // serialized), but deny it rather than silently replacing the first.
  if (pending.has(req.chatKey)) {
    finish({ behavior: "deny", message: "another approval is already pending" });
    return;
  }

  const timer = setTimeout(() => {
    pending.delete(req.chatKey);
    finish({
      behavior: "deny",
      message: `approval timed out after ${config.approvalTimeoutMs / 60000}m; re-send your message to retry`,
    });
    sendPrompt(req.chatKey, "⏱ approval timed out — denied.");
  }, config.approvalTimeoutMs);

  const questions = kind === "question" ? readQuestions(req.input) : [];
  pending.set(req.chatKey, {
    ...req,
    kind: kind === "question" && !questions.length ? "tool" : kind,
    queue: questions,
    answers: {},
    total: questions.length,
    resolve: (verdict) => {
      clearTimeout(timer);
      finish(verdict);
    },
  });

  sock.on("close", () => {
    // claude died or was !stopped while waiting; clear the prompt.
    if (pending.get(req.chatKey)?.input === req.input) {
      clearTimeout(timer);
      pending.delete(req.chatKey);
    }
  });

  if (questions.length) {
    ask(req.chatKey, renderQuestion(questions[0], 0, questions.length));
    return;
  }
  if (kind === "plan") {
    ask(req.chatKey, renderPlan(planTextOf(req.input)));
    return;
  }

  ask(
    req.chatKey,
    `Claude wants: ${describeTool(req.toolName, req.input)}\n` +
      `Reply 1 allow · 2 deny · 3 allow all ${req.toolName} this session\n` +
      `(or react 👍 allow · 👎 deny · 💯 allow all)`,
  );
}

/** Questions out of an AskUserQuestion input, defensively — a malformed or
 * empty list falls back to the plain 1/2/3 prompt rather than stranding the
 * run with a question nobody can answer. */
function readQuestions(input: unknown): Question[] {
  const qs = (input as { questions?: unknown })?.questions;
  if (!Array.isArray(qs)) return [];
  return qs.filter(
    (q): q is Question =>
      Boolean(q) && typeof (q as Question).question === "string",
  );
}

/** ExitPlanMode's plan text. Newer builds read the plan from a file and send
 * nothing, so fall back to a pointer rather than an empty message. */
function planTextOf(input: unknown): string {
  const plan = (input as { plan?: unknown })?.plan;
  return typeof plan === "string" && plan.trim()
    ? plan
    : "(claude wrote the plan to its plan file — ask it to show the plan if you need it here)";
}

// ---------------------------------------------------------------------------
// Answering by reaction
// ---------------------------------------------------------------------------

/**
 * The digit a reaction stands for, or "" if the emoji means nothing here.
 *
 * Typing 1/2/3 is fine until two prompts are open, when the digit is ambiguous
 * and the wrong one gets answered. A reaction names the message it answers, so
 * it can't be misrouted — and on a phone it is one tap instead of a keyboard.
 */
export function reactionAnswer(emoji: string): string {
  // Skin-tone modifiers and the variation selector are presentation, not
  // meaning: 👍🏽 and 👍️ are the same answer as 👍.
  const bare = emoji.replace(/[\u{1F3FB}-\u{1F3FF}️‍]/gu, "");
  if (["👍", "✅", "☑", "👌", "🆗"].includes(bare)) return "1";
  if (["👎", "❌", "✖", "🚫", "⛔"].includes(bare)) return "2";
  if (["💯", "♾", "🔁", "🔂"].includes(bare)) return "3";
  return "";
}
