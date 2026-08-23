import fs from "node:fs";
import path from "node:path";
import { attachmentPreamble, extractSendMarkers } from "./attachments.ts";
import {
  answerPending,
  hasPending,
  pendingKind,
  pendingPromptRef,
  reactionAnswer,
} from "./approvals.ts";
import { replyPrefix } from "./chunk.ts";
import {
  atCapacity,
  type BackgroundTask,
  describeTasks,
  handOff,
  isRunning,
  isWaiting,
  latestSessionId,
  type RunOverrides,
  type RunResult,
  runClaude,
  stop,
  waitingOn,
} from "./claude.ts";
import { scheduleStatus, setScheduleRunner } from "./schedules.ts";
import {
  groupRateAllows,
  groupRateResetMinutes,
  isGroupChat,
  recordGroupMessage,
  takeGroupContext,
} from "./chat.ts";
import { config, isWithinCwdRoots } from "./config.ts";
import { isBashRunning, runBash, stopBash } from "./bash.ts";
import {
  autoActive,
  autoExpired,
  DEFAULT_SESSION,
  getChat,
  listSessions,
  sessionName,
  switchSession,
  updateChat,
} from "./state.ts";
import { createStatus, type Status } from "./status.ts";
import {
  attempt as attemptUnlock,
  isFresh,
  isUnlocked,
  lock,
  looksLikeCode,
  refresh,
  touch,
  unlockEnabled,
  unlockSummary,
  verifyCode,
} from "./unlock.ts";
import {
  type MsgRef,
  overflowSize,
  sendFileTo,
  reactTo,
  refIdOf,
  sendReply,
  sendTo,
  takeOverflow,
} from "./transport.ts";
import { usageReport } from "./usage.ts";
import {
  armWakeup,
  cancelWakeup,
  pendingWakeup,
  setWakeupRunner,
} from "./wakeup.ts";

export { registerTransport, sendTo, type MsgRef, type Transport } from "./transport.ts";

// Aliases accepted by !model, so a phone doesn't have to type a full model id.
const MODEL_ALIASES: Record<string, string> = {
  opus: "claude-opus-5",
  sonnet: "claude-sonnet-5",
  haiku: "claude-haiku-4-5",
  fable: "claude-fable-5",
};
const EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"];

// One in-flight claude per chat; extra messages wait their turn. The inbound
// ref rides along so the reaction can be moved from 🕒 to 👀 to ✅ on the
// message that asked for it, rather than on whatever arrived most recently.
interface Queued {
  body: string;
  ref?: MsgRef;
  /** Paths of files that came with this message. */
  files?: string[];
  /** Already told the sender it is waiting on the global concurrency cap, so
   * the 3s re-check doesn't re-send the same reaction every time round. */
  waiting?: boolean;
  /** This entry is a scheduled wake-up firing, not something the person typed.
   * The run gets told so, and there is no inbound message to react to. */
  wakeup?: boolean;
  /** Pinned cwd/session for a gateway-scheduled run — or a wake-up such a run
   * armed. The run happens in this thread regardless of where the chat's
   * `!use`/`!cwd` currently point. */
  run?: RunOverrides;
  /** Name of the gateway schedule behind this entry, for its preamble. */
  schedName?: string;
}
const queues = new Map<string, Queued[]>();
let droppedLog = 0;

function enqueueScheduled(chatKey: string, item: Queued): void {
  const queue = queues.get(chatKey) ?? [];
  if (queue.length >= config.queueDepth) {
    void sendTo(chatKey, "⚠ queue full — scheduled wake-up dropped");
    return;
  }
  queue.push(item);
  queues.set(chatKey, queue);
  if (queue.length === 1 && (!isRunning(chatKey) || isWaiting(chatKey)))
    void drain(chatKey);
}

// A wake-up firing is a message from the schedule, not from the person. It
// enters the same per-chat queue an inbound message does — so it hands off to
// a parked run exactly like a typed message would, queues behind a busy one,
// and its reply goes out with the usual banner.
setWakeupRunner((chatKey, wakeup) =>
  enqueueScheduled(chatKey, {
    body: wakeup.prompt,
    wakeup: true,
    run: wakeup.run,
    schedName: wakeup.schedName,
  }),
);

// A gateway schedule firing (values.yaml messaging.schedules). Same queue,
// same quiet-on-empty delivery as a wake-up; what differs is that the timer
// belongs to the gateway and the run is pinned to its own cwd and session.
setScheduleRunner((spec, chatKey) =>
  enqueueScheduled(chatKey, {
    body: spec.prompt,
    wakeup: true,
    schedName: spec.name,
    run: {
      cwd: spec.cwd,
      session: spec.session,
      fresh: spec.fresh,
      model: spec.model,
      effort: spec.effort,
    },
  }),
);

/** What a wake-up run is told about why it is running. */
const WAKEUP_PREAMBLE =
  "[Scheduled wake-up: this run was started by your own ScheduleWakeup call, " +
  "not by a new message from the user. The prompt below is the one you " +
  "scheduled.]";

/** And what a gateway-scheduled run is told. The distinction matters: this
 * run never armed anything and must not believe it owes the user an answer —
 * a turn with nothing to report should end with no text at all. */
function preambleFor(item: Queued): string {
  if (!item.wakeup) return "";
  if (!item.schedName) return WAKEUP_PREAMBLE;
  return (
    `[Scheduled run "${item.schedName}": started by the gateway's recurring ` +
    "schedule, not by a message from the user. If there is nothing that " +
    "needs the user's attention, end your turn with no text.]"
  );
}

/** Reaction state machine on the sender's own message. Both networks replace a
 * previous reaction from the same account, so each call is the whole update. */
function react(chatKey: string, ref: MsgRef | undefined, emoji: string): void {
  if (!config.reactions.enabled) return;
  void reactTo(chatKey, ref, emoji);
}

/** The status message for the run currently draining this chat, so SIGTERM can
 * tell it why it is about to stop. A run that outlives its first answer gets a
 * fresh Status per turn, so this is replaced as the run goes, not set once. */
const statuses = new Map<string, Status>();

/**
 * The inbound message a run's NEXT reply answers, so the ✅ lands on it.
 *
 * A run no longer maps one-to-one onto a message: it can wake itself when
 * background work finishes, and it can be handed a second message while it is
 * parked. Only a turn that some message actually asked for reacts — a wake-up
 * answers the build, not the phone, and there is nothing there to react to.
 */
const deliveryRefs = new Map<string, MsgRef | undefined>();
/** A person's message is waiting on the next turn's reply. Off for gateway
 * wake-ups and the CLI's own resumptions, whose turns may say nothing. */
const awaitingReply = new Map<string, boolean>();

export function activeStatus(chatKey: string): Status | undefined {
  return statuses.get(chatKey);
}

export interface InboundMeta {
  /** Display label for the sender, used for group context lines. */
  sender: string;
  /**
   * This message is permitted to drive a run. In a 1:1 that means the sender is
   * on the personal allowlist; in a group it means the GROUP is allowlisted —
   * any member of an approved room qualifies.
   */
  allowed: boolean;
  /**
   * Sender is on the personal allowlist. Required for `!` commands anywhere:
   * a group grant is permission to ask the bot things, not to reconfigure it.
   */
  owner: boolean;
  /** The bot was tagged in this message (groups only). */
  mentioned: boolean;
  /** Absolute paths of files that came with this message, already saved. */
  files?: string[];
  /** Handle on the inbound message itself, for reacting to it. Both transports
   * already hold this (it is what the read receipt is addressed to); it is
   * optional only so a surface without message ids stays valid. */
  ref?: MsgRef;
}

/**
 * An inbound reaction on one of the bot's own messages. Only one thing
 * currently answers to this: a 👍/👎 on an open permission prompt.
 *
 * Deliberately narrow. A reaction is not a message — it cannot start a run,
 * change settings or reach claude — so the blast radius is exactly the prompt
 * that is already on screen waiting for a 1/2/3.
 */
export function handleReaction(
  chatKey: string,
  targetId: string | undefined,
  emoji: string,
  meta: { owner: boolean },
): void {
  // Answering a permission prompt is a privileged act, so it takes the same
  // credential as a `!` command: membership of an allowlisted group is not it.
  if (!meta.owner || !targetId) return;
  if (!hasPending(chatKey)) return;

  // The reaction has to be ON the prompt. Otherwise a 👍 on some older message
  // would silently approve whatever happens to be pending now.
  const promptId = refIdOf(chatKey, pendingPromptRef(chatKey));
  if (!promptId || promptId !== targetId) return;

  const answer = reactionAnswer(emoji);
  if (!answer) return;
  const kind = pendingKind(chatKey);
  if (answerPending(chatKey, answer)) {
    console.log(`${chatKey}: prompt answered ${answer} by reaction ${emoji}`);
    // Same handover as a typed answer — a 👍 on a plan is still minutes of work
    // starting, and there is no inbound message here to react back to.
    if (kind !== "tool") void activeStatus(chatKey)?.restart();
  }
}

/**
 * Feed a reply to the open prompt, and — if it took — say so.
 *
 * A prompt answer is the one kind of message that reaches claude without going
 * through the queue, so it used to get none of the queue's feedback: no
 * reaction on what you typed, and a status message already stranded above the
 * prompt. On a plan, where the reply buys minutes of replanning, that reads as
 * the gateway having dropped it. So the reply gets the same 👀 an ordinary
 * message gets, and the run's status moves to a fresh message below it.
 */
function answerReceived(
  chatKey: string,
  body: string,
  ref: MsgRef | undefined,
): boolean {
  const kind = pendingKind(chatKey);
  if (!answerPending(chatKey, body)) return false;
  react(chatKey, ref, config.reactions.working);
  // Not for a plain 1/2/3 on a tool: that resumes instantly, and a second
  // status message per approval would bury the thread on a busy run.
  if (kind !== "tool") void activeStatus(chatKey)?.restart();
  return true;
}

export function handleInbound(
  chatKey: string,
  text: string,
  meta: InboundMeta,
): void {
  const body = text.trim();
  // A photo with no caption is still a message; only a genuinely empty one
  // (a receipt, a reaction we don't act on) is nothing.
  if (!body && !meta.files?.length) return;

  if (isGroupChat(chatKey)) {
    // Everything said in the room becomes context, whoever said it — that is
    // the point of a group, and answering "what did we land on?" needs the
    // messages the bot was never addressed in.
    recordGroupMessage(chatKey, meta.sender, body);
    if (!meta.allowed) return;
    if (config.groups.requireMention && !meta.mentioned) {
      // Logged because the silent version of this is genuinely hard to
      // diagnose: typing "@claude" by hand does NOT create a Signal mention —
      // only picking the bot from the autocomplete does — and the two are
      // indistinguishable in the chat UI.
      console.log(`group: ${meta.sender} not a mention, skipped`);
      return;
    }
    if (!groupRateAllows(chatKey)) {
      void sendTo(
        chatKey,
        `⚠ group limit reached (${config.groups.rateLimit} per ` +
          `${config.groups.rateWindowMs / 60_000}m); resets in ` +
          `~${groupRateResetMinutes(chatKey)}m`,
      );
      return;
    }
  } else if (!meta.allowed) {
    return;
  } else if (unlockEnabled()) {
    // 1:1 only. A group has no filesystem, no shell and no `!` commands, and
    // the room — not a person — is its credential, so there is nothing for a
    // code to attest to and no one phone to type it.
    if (!isUnlocked(chatKey)) {
      const result = attemptUnlock(chatKey, body);
      void sendTo(chatKey, result.reply);
      if (result.status === "unlocked")
        console.log(`${chatKey}: unlocked by TOTP`);
      // Either way this message is spent: a code is not also a prompt, and
      // anything else arrived while locked and is not going to claude.
      return;
    }
    // A code sent into an already-open chat re-stamps freshness, which is how
    // "code, then !bash" works when the session has been open all day. Guarded
    // on hasPending so it can never eat an answer to a permission prompt —
    // those are 1/2/3, but a plan reply is free text and could be anything.
    if (looksLikeCode(body) && !hasPending(chatKey)) {
      const ok = verifyCode(body) !== null;
      if (ok) refresh(chatKey);
      void sendTo(
        chatKey,
        ok
          ? `🔓 code accepted — !bash / !auto for the next ` +
              `${Math.round(config.unlock.freshMs / 60_000)}m`
          : "🔒 wrong code",
      );
      return;
    }
    touch(chatKey);
  }

  // Digit replies feed a pending permission prompt, never claude.
  if (hasPending(chatKey) && answerReceived(chatKey, body, meta.ref)) return;

  if (body.startsWith("!")) {
    // Owner-only, everywhere. Membership of an allowlisted group buys the
    // right to ask the bot things, not to repoint its cwd, switch its model, or
    // wipe its session — and in a shared room those would be everyone's
    // settings, changed by one person.
    if (!meta.owner) {
      console.log(`group: ignoring ${body.split(/\s+/)[0]} from ${meta.sender}`);
      return;
    }
    void handleCommand(chatKey, body);
    return;
  }

  const queue = queues.get(chatKey) ?? [];
  if (queue.length >= config.queueDepth) {
    react(chatKey, meta.ref, config.reactions.error);
    void sendTo(chatKey, `⚠ queue full (${config.queueDepth}); message dropped`);
    return;
  }
  queue.push({ body, ref: meta.ref, files: meta.files });
  queues.set(chatKey, queue);
  // 🕒 means "yours is next"; drain() swaps it for 👀 when the run actually
  // starts. That distinction is the whole reason a reaction beats a read
  // receipt here — and the reason only the waiting case reacts from here, so a
  // message that starts immediately gets one reaction rather than two.
  //
  // isWaiting is the second door: a run parked on background work IS running,
  // but it is idle, and drain() can hand the message straight to it rather
  // than sit on it until the build it is waiting for reports back.
  if (queue.length === 1 && (!isRunning(chatKey) || isWaiting(chatKey)))
    void drain(chatKey);
  else react(chatKey, meta.ref, config.reactions.queued);
}

/** What a message with nothing but attachments is handed to the model as. */
const NO_CAPTION = "(the user sent this with no caption)";

async function drain(chatKey: string): Promise<void> {
  const queue = queues.get(chatKey);
  if (!queue?.length) return;
  // Re-entrancy guard. drain() used to be reachable only when nothing was
  // running; it is now also called for a parked run, so a mid-turn call (the
  // capacity re-check, a second message) has to bow out on its own.
  if (isRunning(chatKey) && !isWaiting(chatKey)) return;

  // A parked run is idle with an open stdin, so the cheapest thing to do with
  // the next message is give it to the run that is already up — which also
  // keeps its background work alive, where starting a fresh run would mean
  // ending this one and killing the tasks with it.
  if (isWaiting(chatKey)) {
    const item = queue[0];
    // A pinned run is never handed to a parked one: the parked run is some
    // other thread's context (or last firing's session), and injecting a
    // scheduled prompt there is exactly the cross-thread leak pinning exists
    // to prevent. Wait the park out and try again.
    if (item.run) {
      setTimeout(() => void drain(chatKey), 60_000);
      return;
    }
    const handOffPreamble = [
      preambleFor(item),
      attachmentPreamble(item.files ?? []),
    ]
      .filter(Boolean)
      .join("\n\n");
    if (handOff(chatKey, item.body || NO_CAPTION, handOffPreamble)) {
      queue.shift();
      deliveryRefs.set(chatKey, item.ref);
      awaitingReply.set(chatKey, !item.wakeup);
      react(chatKey, item.ref, config.reactions.working);
      // The parked run's onTurn below delivers this reply; there is no second
      // run to start and nothing here to await.
      return;
    }
  }

  if (atCapacity()) {
    // Re-check shortly; global cap bounds worst-case pod memory. Say so once —
    // this chat is next in line but another chat holds the slot.
    if (!queue[0].waiting) {
      queue[0].waiting = true;
      react(chatKey, queue[0].ref, config.reactions.queued);
    }
    setTimeout(() => void drain(chatKey), 3000);
    return;
  }
  const item = queue.shift()!;
  const { body: message, ref, files, wakeup, run: runOverrides } = item;
  // Say when the grant lapsed rather than just quietly prompting again — the
  // difference between "auto is off now" and "why is it suddenly asking me?".
  if (autoExpired(getChat(chatKey))) {
    updateChat(chatKey, { auto: false, autoUntil: undefined });
    await sendTo(chatKey, "⏱ auto mode expired — mutations will prompt again");
  }
  react(chatKey, ref, config.reactions.working);
  deliveryRefs.set(chatKey, ref);
  awaitingReply.set(chatKey, !wakeup);
  const group = isGroupChat(chatKey);
  // No status message in a group: the room did not ask to watch the bot's tool
  // calls, and a group run is restricted to WebFetch/WebSearch anyway, so there
  // is very little to watch.
  let status = group ? undefined : createStatus(chatKey);
  if (status) {
    statuses.set(chatKey, status);
    await status.begin();
  }
  try {
    // The attachment preamble goes in front of the group context, so the
    // files are the first thing the run reads about.
    const preamble = [
      preambleFor(item),
      attachmentPreamble(files ?? []),
      group ? takeGroupContext(chatKey) : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    const final = await runClaude(chatKey, message || NO_CAPTION, preamble, {
      onEvent: (ev) => {
        // A new turn on a run that already answered once — the CLI woke itself
        // for a finished background task, or was handed a second message. The
        // previous turn's status was collapsed into its receipt, so the
        // continuation needs a live one of its own, below the reply.
        if (
          !group &&
          ev.type === "system" &&
          ev.subtype === "init" &&
          status &&
          !status.active
        ) {
          status = createStatus(chatKey);
          statuses.set(chatKey, status);
          void status.begin();
        }
        status?.onEvent(ev);
      },
      onTurn: async (result, _turn, pending) => {
        // The turn's own wake-up request, applied before its reply goes out so
        // "next check at …" in the text is true by the time it is read. Only
        // what THIS turn asked for (WakeupTracker.take) — arming is not
        // re-affirmed turn to turn, so a spent wake-up can't come back.
        if (result.wakeup === "stop") cancelWakeup(chatKey);
        // A pinned run's wake-up inherits the pin (resuming, not fresh — the
        // follow-up continues the session that asked for it), so an intraday
        // "check back in an hour" lands in the schedule's own thread.
        else if (result.wakeup)
          armWakeup(chatKey, {
            ...result.wakeup,
            run: runOverrides && { ...runOverrides, fresh: false },
            schedName: item.schedName,
          });
        // Collapse the status before the answer lands, so the thread reads
        // status-receipt-then-answer rather than answer-then-a-stale-"working…".
        // The receipt says what is still outstanding, which is the difference
        // between an answer and an interim report.
        await status?.finish(
          !result.isError,
          pending.length ? `⏳ ${describeTasks(pending)}` : "",
        );
        // A turn with no words and nobody waiting on it — the CLI resuming
        // after a stale task notification ("continue from where you left
        // off") and finding nothing to say — gets no message. A bare banner
        // reading "(no result text)" is noise on a phone. A turn that answers
        // a real message still goes out, so silence never eats a reply.
        const unprompted = !awaitingReply.get(chatKey);
        awaitingReply.set(chatKey, false);
        if (!result.text && !result.isError && unprompted) return;
        await deliver(chatKey, result, group, runOverrides);
        // If this turn parked the run and something is queued behind it, the
        // park is a chance to answer it — handleInbound only kicks drain for
        // the first message, so a second one that arrived during the last park
        // would otherwise wait out the whole run.
        if (isWaiting(chatKey) && queues.get(chatKey)?.length)
          void drain(chatKey);
      },
      onSelfWake: () => {
        // The parked child's own ScheduleWakeup timer fired in-process. The
        // gateway's persisted copy of that wake-up is spent — the wake turn's
        // onTurn re-arms if the model schedules another.
        cancelWakeup(chatKey);
      },
      onAbandon: (tasks, reason) => {
        // These die with the process, and nothing else in the thread would
        // ever say so — the last thing the chat heard was "I'll report back".
        void sendTo(
          chatKey,
          `⚠ gave up waiting on ${describeTasks(tasks)} ` +
            (reason === "timeout"
              ? `after ${formatDuration(config.background.waitMs)}`
              : `(${config.background.maxTurns}-turn limit)`) +
            " — it was stopped, not finished. Ask again to pick it back up.",
        );
      },
    }, runOverrides);
    // A run that died mid-turn never reached onTurn, so nothing above has
    // collapsed its status. Left alone, the ticker keeps editing and rolling
    // "working…" messages for a process that is gone — which is what made a
    // !stop look like a run that wouldn't die.
    if (status?.active) await status.finish(false, final.aborted ? "stopped" : "");
    // And nothing has answered the message that started it. Say how it ended,
    // so the thread doesn't just go quiet (the old "claude exited 143").
    if (final.aborted) await deliver(chatKey, final, group, runOverrides);
  } catch (err) {
    await status?.replace(`⚠ gateway error`);
    // Whatever this run still owes a reaction to. Undefined once a turn has
    // been delivered — that message already has its ✅ and is not this error's.
    react(chatKey, deliveryRefs.get(chatKey), config.reactions.error);
    await sendTo(chatKey, `⚠ gateway error: ${String(err)}`);
  } finally {
    statuses.delete(chatKey);
    deliveryRefs.delete(chatKey);
    if (queue.length) void drain(chatKey);
  }
}

/**
 * One turn's answer, out to the chat.
 *
 * Called once per turn rather than once per run: a run that parks on
 * background work answers now ("build started") and again when it wakes, and
 * both are ordinary replies. The ✅ goes on whichever message asked — a
 * wake-up answers nobody's message and reacts to nothing.
 */
async function deliver(
  chatKey: string,
  result: RunResult,
  group: boolean,
  run?: RunOverrides,
): Promise<void> {
  const ref = deliveryRefs.get(chatKey);
  if (ref !== undefined) {
    react(
      chatKey,
      ref,
      result.isError ? config.reactions.error : config.reactions.done,
    );
    // Spent: the next turn is either a wake-up (nobody's message) or a message
    // that will set its own.
    deliveryRefs.set(chatKey, undefined);
  }
  // A group reply is just an answer in a conversation — the cwd/session/auto
  // banner is workspace bookkeeping and means nothing to the other people in
  // the room, so it stays on the 1:1 surface.
  // Only a 1:1 honours a send marker. In a group the members are not on the
  // personal allowlist and claude has no filesystem there anyway — but the
  // marker is parsed from text, and text is the one thing a room can steer.
  const text = result.text || "(no result text)";
  const outbound =
    config.attachments.enabled && !group
      ? extractSendMarkers(text)
      : { text, files: [], problems: [] };

  if (group) {
    await sendReply(chatKey, `${result.isError ? "⚠ " : ""}${outbound.text}`);
  } else {
    const chat = getChat(chatKey);
    // A pinned run's banner names ITS thread — cwd, session slot, and the
    // session id the run actually produced — not wherever the chat points.
    const prefix = replyPrefix(
      run?.cwd ?? chat.cwd,
      run ? result.sessionId : chat.sessionId,
      chatMode(chatKey),
      run?.session ?? sessionName(chat),
    );
    await sendReply(
      chatKey,
      `${prefix}${result.isError ? " ⚠" : ""}\n${outbound.text}`,
    );
  }
  // After the text, so the words arrive first on a slow link.
  for (const file of outbound.files) {
    const err = await sendFileTo(chatKey, file);
    if (err) outbound.problems.push(`⚠ couldn't send ${file}: ${err}`);
  }
  if (outbound.problems.length)
    await sendTo(chatKey, outbound.problems.join("\n"));
}

/** The chat's permission stance, for the banner and !status. Empty string is
 * the default one: prompt over chat before anything mutates. */
function chatMode(chatKey: string): string {
  const chat = getChat(chatKey);
  if (chat.plan) return "plan";
  if (!autoActive(chat)) return "";
  // Show what's left, so the banner on every reply is also the countdown.
  return chat.autoUntil
    ? `auto ${formatDuration(chat.autoUntil - Date.now())}`
    : "auto";
}

/** `30m`, `2h`, `90` (minutes). Null when it isn't a duration at all. */
export function parseDuration(arg: string): number | null {
  const m = /^(\d+)\s*(m|min|h|hr|hour)?s?$/i.exec(arg.trim());
  if (!m) return null;
  const n = Number(m[1]);
  if (!n) return null;
  const unit = (m[2] ?? "m").toLowerCase();
  const ms = unit.startsWith("h") ? n * 3_600_000 : n * 60_000;
  // A day of unattended root is not a time box; refuse rather than pretend.
  return ms > 86_400_000 ? null : ms;
}

/** Coarse on purpose — "1h20m left" is what you want to read on a phone. */
export function formatDuration(ms: number): string {
  const mins = Math.max(1, Math.round(ms / 60_000));
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${h}h${rem}m` : `${h}h`;
}

// Returns whatever the last sendTo did — the `return sendTo(...)` shape below
// is how each case says "reply and stop", and the handle it hands back is of no
// interest to the caller.
async function handleCommand(chatKey: string, body: string): Promise<unknown> {
  const [cmd, ...rest] = body.split(/\s+/);
  const arg = rest.join(" ");
  // Everything after the command verbatim — `!bash` needs the original
  // spacing, quoting and newlines, which the split above flattens.
  const rawArg = body.slice(cmd.length).trim();
  const chat = getChat(chatKey);

  // Escalation needs a code entered in the last `unlock.freshMs`, not merely an
  // open session. `!bash` is a shell with no model in the loop and `!auto` is a
  // standing grant to run anything unprompted — both are worth re-asking "are
  // you actually holding the phone?" for, which a window that slid open hours
  // ago does not answer. `!auto off` is exempt: turning a grant OFF is never
  // the thing to gate.
  const escalating =
    cmd === "!bash" || (cmd === "!auto" && arg !== "off" && arg !== "");
  if (escalating && !isFresh(chatKey)) {
    return sendTo(
      chatKey,
      `🔒 ${cmd} needs a fresh code — send your 6 digits, then repeat the command ` +
        `(valid ${Math.round(config.unlock.freshMs / 1000)}s)`,
    );
  }

  switch (cmd) {
    case "!lock":
      // The panic button, and the reason the unlock notice tells you about it:
      // if a 🔓 lands on your phone and it wasn't you, this is the reply.
      lock(chatKey);
      return sendTo(
        chatKey,
        unlockEnabled()
          ? "🔒 locked — the next message needs a code"
          : "unlock is not configured (no TOTP secret), so there is nothing to lock",
      );
    case "!unlock":
      // Bare `!unlock` inside an open session re-stamps freshness, so the
      // shape "code, then !bash" also works as "!unlock, code, !bash".
      return sendTo(
        chatKey,
        !unlockEnabled()
          ? "unlock is not configured (no TOTP secret)"
          : "send your 6-digit code as its own message",
      );
    case "!new":
    case "!clear":
      // Drops the session pointer, so the next message starts a run with no
      // history. The old transcript stays on the PVC under ~/.claude — this
      // forgets it, it doesn't delete it, and `!resume <id>` can still reach it.
      updateChat(chatKey, { sessionId: undefined });
      return sendTo(chatKey, "✓ history cleared — next message starts fresh");
    case "!model": {
      if (!arg)
        return sendTo(
          chatKey,
          `model: ${chat.model ?? config.model} (default ${config.model})\n` +
            `usage: !model ${Object.keys(MODEL_ALIASES).join("|")}|<model-id>|default`,
        );
      if (arg === "default") {
        updateChat(chatKey, { model: undefined });
        return sendTo(chatKey, `✓ model back to default (${config.model})`);
      }
      const model = MODEL_ALIASES[arg] ?? arg;
      updateChat(chatKey, { model });
      return sendTo(chatKey, `✓ model ${model} (applies to the next message)`);
    }
    case "!effort": {
      if (!arg)
        return sendTo(
          chatKey,
          `effort: ${chat.effort ?? config.effort} (default ${config.effort})\n` +
            `usage: !effort ${EFFORT_LEVELS.join("|")}|default`,
        );
      if (arg === "default") {
        updateChat(chatKey, { effort: undefined });
        return sendTo(chatKey, `✓ effort back to default (${config.effort})`);
      }
      if (!EFFORT_LEVELS.includes(arg))
        return sendTo(chatKey, `usage: !effort ${EFFORT_LEVELS.join("|")}`);
      updateChat(chatKey, { effort: arg });
      return sendTo(chatKey, `✓ effort ${arg}`);
    }
    case "!resume": {
      const id = arg || latestSessionId(chat.cwd);
      if (!id) return sendTo(chatKey, `no sessions found for ${chat.cwd}`);
      updateChat(chatKey, { sessionId: id });
      return sendTo(chatKey, `✓ resuming ${id.slice(0, 8)} in ${chat.cwd}`);
    }
    case "!cwd": {
      if (!arg) return sendTo(chatKey, `cwd: ${chat.cwd}`);
      const target = path.resolve(
        arg.startsWith("/") ? arg : path.join(config.codeRoot, arg),
      );
      // Containment before existence: a scoped instance should say the same
      // thing about /etc/shadow whether or not it is there.
      if (!isWithinCwdRoots(target))
        return sendTo(
          chatKey,
          `⚠ ${target} is outside this instance's allowed directories ` +
            `(${config.cwdRoots.join(", ")})`,
        );
      if (!fs.existsSync(target))
        return sendTo(chatKey, `⚠ no such directory: ${target}`);
      updateChat(chatKey, { cwd: target, sessionId: undefined });
      return sendTo(chatKey, `✓ cwd ${target} (session cleared)`);
    }
    case "!auto": {
      // Disabled instances refuse even `!auto off`, so the reply never implies
      // the mode exists here and is merely currently unset.
      if (!config.autoEnabled)
        return sendTo(
          chatKey,
          "⚠ auto mode is disabled on this instance — mutations always prompt",
        );
      if (arg === "off") {
        updateChat(chatKey, { auto: false, autoUntil: undefined });
        return sendTo(chatKey, "✓ auto mode off — mutations will prompt");
      }
      // A duration is the recommended form. This pod is cluster-admin with
      // root on every node, and `!auto on` is a standing grant that outlives
      // the reason it was given — a phone left unlocked inherits it.
      const ms = arg === "on" ? 0 : parseDuration(arg);
      if (ms === null)
        return sendTo(
          chatKey,
          "usage: !auto <30m|2h> · !auto on (no expiry) · !auto off",
        );
      const autoUntil = ms ? Date.now() + ms : undefined;
      // Auto and plan are opposite answers to the same question, so setting
      // one clears the other (see !plan).
      updateChat(chatKey, { auto: true, autoUntil, plan: false });
      return sendTo(
        chatKey,
        `⚡ auto mode ON — tools run without asking, ` +
          (autoUntil ? `for ${formatDuration(ms)}` : "until !auto off") +
          (chat.plan ? " (plan off)" : ""),
      );
    }
    case "!plan": {
      // Bare `!plan` turns it on: on a phone the whole value is typing four
      // characters before a question you don't want acted on.
      const on = arg === "" || arg === "on";
      if (!on && arg !== "off") return sendTo(chatKey, "usage: !plan [on|off]");
      // Clearing auto is the point, not a side effect — "don't touch anything"
      // and "don't ask before touching" cannot both be the rule.
      updateChat(chatKey, { plan: on, auto: on ? false : chat.auto });
      return sendTo(
        chatKey,
        on
          ? "📋 plan mode ON — claude researches and proposes, no edits" +
              (chat.auto ? " (auto off)" : "")
          : "✓ plan mode off",
      );
    }
    case "!bash": {
      if (!config.bash.enabled)
        return sendTo(chatKey, "⚠ !bash is disabled (messaging.bash.enabled)");
      // Not in a group, for the same reason approvals aren't: the room reads
      // every byte, and the room's members are not on the personal allowlist.
      if (isGroupChat(chatKey))
        return sendTo(chatKey, "⚠ !bash is not available in group chats");
      if (!rawArg) return sendTo(chatKey, "usage: !bash <command>");
      if (isBashRunning(chatKey))
        return sendTo(chatKey, "⚠ a !bash is already running — !stop to kill");
      const result = await runBash(chatKey, rawArg, chat.cwd);
      const status =
        result.code === 0 ? "" : ` (exit ${result.code ?? "killed"})`;
      return sendReply(chatKey, `$ ${rawArg}${status}\n${result.text}`);
    }
    case "!usage": {
      const days = Number(arg);
      const window =
        Number.isFinite(days) && days > 0 ? Math.min(days, 90) : config.usageDays;
      // Scanning transcripts takes a moment on a busy workspace; say so rather
      // than leave the chat silent.
      await sendTo(chatKey, `reading usage for the last ${window}d…`);
      return sendTo(chatKey, await usageReport(window));
    }
    case "!stop": {
      // Also the no-model lever on a wake-up loop: the polite way out is the
      // model calling ScheduleWakeup with stop, but the impolite way must not
      // require a model that is misbehaving to cooperate.
      const killed = [
        stop(chatKey) ? "claude" : "",
        stopBash(chatKey) ? "bash" : "",
        cancelWakeup(chatKey) ? "scheduled wake-up" : "",
      ].filter(Boolean);
      return sendTo(
        chatKey,
        killed.length ? `✓ stopped ${killed.join(" + ")}` : "nothing running",
      );
    }
    case "!use": {
      if (!arg)
        return sendTo(
          chatKey,
          `session: ${sessionName(chat)}\nusage: !use <name> — switch or start a thread`,
        );
      // One word, so a name can't be confused with the rest of a command.
      const name = arg.split(/\s+/)[0];
      if (!/^[\w-]{1,24}$/.test(name))
        return sendTo(chatKey, "usage: !use <name> (letters, digits, - or _)");
      const { resumed } = switchSession(chatKey, name);
      return sendTo(
        chatKey,
        resumed
          ? `✓ back on "${name}" — it picks up where it left off`
          : `✓ started "${name}" — a fresh thread; the old one is parked`,
      );
    }
    case "!sessions": {
      const lines = listSessions(chat).map(
        (s) =>
          `${s.current ? "▸" : " "} ${s.name}` +
          (s.sessionId ? ` · ${s.sessionId.slice(0, 6)}` : " · (new)"),
      );
      return sendTo(
        chatKey,
        [...lines, "!use <name> to switch or start one"].join("\n"),
      );
    }
    case "!more": {
      const rest = takeOverflow(chatKey);
      if (!rest) return sendTo(chatKey, "nothing more to show");
      // Back through sendReply, so a remainder that is itself too long leaves
      // its own remainder and !more just keeps working.
      return sendReply(chatKey, rest);
    }
    case "!status": {
      const q = queues.get(chatKey)?.length ?? 0;
      const wake = pendingWakeup(chatKey);
      return sendTo(
        chatKey,
        [
          ...(wake
            ? [`wake-up: in ${formatDuration(wake.at - Date.now())} (!stop cancels)`]
            : []),
          ...scheduleStatus().map(
            (s) =>
              `schedule ${s.name}: ` +
              (s.next ? `next in ${formatDuration(s.next.getTime() - Date.now())}` : "never"),
          ),
          `cwd: ${chat.cwd}`,
          `session: ${sessionName(chat)} · ${chat.sessionId ?? "(none)"}`,
          `model: ${chat.model ?? config.model} · effort: ${chat.effort ?? config.effort}`,
          isGroupChat(chatKey)
            ? `group: tools limited to ${config.groups.allowedTools}`
            : `mode: ${chatMode(chatKey) || "prompt on mutations"}`,
          ...(isGroupChat(chatKey) ? [] : [unlockSummary(chatKey)]),
          // "running" and "parked" are different answers to "why am I
          // waiting?" — parked means it already replied and is holding an
          // open process for a background task to report.
          `state: ${
            isWaiting(chatKey)
              ? `parked on ${describeTasks(waitingOn(chatKey))}`
              : isRunning(chatKey)
                ? "running"
                : "idle"
          }${q ? `, ${q} queued` : ""}` +
            (isBashRunning(chatKey) ? ", bash running" : "") +
            (overflowSize(chatKey) ? `, ${overflowSize(chatKey)} chars unsent (!more)` : ""),
        ].join("\n"),
      );
    }
    case "!help":
      return sendTo(
        chatKey,
        "!new/!clear · !resume [id] · !cwd <repo|path> · !auto on|off · " +
          "!plan [on|off] · !model <name> · !effort <level> · !stop · !status\n" +
          "!auto takes a duration too: !auto 30m, !auto 2h\n" +
          "!more shows the rest of a reply that was cut short\n" +
          "!use <name> / !sessions — several threads in one chat\n" +
          "!bash <cmd> shell in the current cwd, no model · !usage [days] tokens\n" +
          "Long jobs run in the background: you get a reply now and a second " +
          "one when they finish. !status shows what a chat is parked on\n" +
          "!lock locks the chat now; a 6-digit code unlocks it. !bash and !auto " +
          "need a code from the last few minutes\n" +
          "During a permission prompt: 1 allow · 2 deny · 3 allow all like it\n" +
          "A ❓ question takes a number or your own words · a 📋 plan takes " +
          "1 to approve, or say what to change",
      );
    default:
      return sendTo(chatKey, `unknown command ${cmd} — try !help`);
  }
}

/**
 * Allowlist gate. Logs (rate-limited) and drops anything unknown.
 *
 * Takes several identifiers because one sender can arrive under more than one —
 * but the callers pass only the ones that AUTHENTICATE (identity.ts): a Signal
 * ACI, or the WhatsApp JID the session is with. Phone numbers and the server's
 * alias fields are filtered out before they get here, because a match on ANY
 * identifier admits the sender and the weakest id would otherwise be the gate.
 */
export function matchesAllowlist(
  sender: string | string[],
  allowed: string[],
): boolean {
  const ids = (Array.isArray(sender) ? sender : [sender]).filter(Boolean);
  return ids.some((id) => allowed.includes(id));
}

/**
 * `logIds` is what gets printed on a drop — everything the envelope said about
 * the sender, including the identifiers that are not allowed to authenticate.
 * Without it, "dropped message from" for a sender who sent only a phone number
 * would print nothing at all.
 */
export function isAllowed(
  sender: string | string[],
  allowed: string[],
  logIds?: string[],
): boolean {
  const ids = (Array.isArray(sender) ? sender : [sender]).filter(Boolean);
  if (matchesAllowlist(ids, allowed)) return true;
  if (droppedLog++ % 20 === 0)
    console.warn(
      `dropped message from non-allowlisted sender ` +
        `${(logIds ?? ids).join(" / ") || "(unidentified)"}`,
    );
  return false;
}
