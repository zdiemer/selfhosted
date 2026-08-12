import fs from "node:fs";
import path from "node:path";
import { answerPending, hasPending } from "./approvals.ts";
import { replyPrefix } from "./chunk.ts";
import {
  atCapacity,
  isRunning,
  latestSessionId,
  runClaude,
  stop,
} from "./claude.ts";
import {
  groupRateAllows,
  groupRateResetMinutes,
  isGroupChat,
  recordGroupMessage,
  takeGroupContext,
} from "./chat.ts";
import { config } from "./config.ts";
import { isBashRunning, runBash, stopBash } from "./bash.ts";
import { getChat, updateChat } from "./state.ts";
import { createStatus, type Status } from "./status.ts";
import {
  type MsgRef,
  overflowSize,
  reactTo,
  sendReply,
  sendTo,
  takeOverflow,
} from "./transport.ts";
import { usageReport } from "./usage.ts";

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
  /** Already told the sender it is waiting on the global concurrency cap, so
   * the 3s re-check doesn't re-send the same reaction every time round. */
  waiting?: boolean;
}
const queues = new Map<string, Queued[]>();
let droppedLog = 0;

/** Reaction state machine on the sender's own message. Both networks replace a
 * previous reaction from the same account, so each call is the whole update. */
function react(chatKey: string, ref: MsgRef | undefined, emoji: string): void {
  if (!config.reactions.enabled) return;
  void reactTo(chatKey, ref, emoji);
}

/** The status message for the run currently draining this chat, so SIGTERM can
 * tell it why it is about to stop. */
const statuses = new Map<string, Status>();

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
  /** Handle on the inbound message itself, for reacting to it. Both transports
   * already hold this (it is what the read receipt is addressed to); it is
   * optional only so a surface without message ids stays valid. */
  ref?: MsgRef;
}

export function handleInbound(
  chatKey: string,
  text: string,
  meta: InboundMeta,
): void {
  const body = text.trim();
  if (!body) return;

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
  }

  // Digit replies feed a pending permission prompt, never claude.
  if (hasPending(chatKey) && answerPending(chatKey, body)) return;

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
  queue.push({ body, ref: meta.ref });
  queues.set(chatKey, queue);
  // 🕒 means "yours is next"; drain() swaps it for 👀 when the run actually
  // starts. That distinction is the whole reason a reaction beats a read
  // receipt here — and the reason only the waiting case reacts from here, so a
  // message that starts immediately gets one reaction rather than two.
  if (queue.length === 1 && !isRunning(chatKey)) void drain(chatKey);
  else react(chatKey, meta.ref, config.reactions.queued);
}

async function drain(chatKey: string): Promise<void> {
  const queue = queues.get(chatKey);
  if (!queue?.length) return;
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
  const { body: message, ref } = queue[0];
  react(chatKey, ref, config.reactions.working);
  const group = isGroupChat(chatKey);
  // No status message in a group: the room did not ask to watch the bot's tool
  // calls, and a group run is restricted to WebFetch/WebSearch anyway, so there
  // is very little to watch.
  const status = group ? undefined : createStatus(chatKey);
  if (status) {
    statuses.set(chatKey, status);
    await status.begin();
  }
  try {
    const result = await runClaude(
      chatKey,
      message,
      group ? takeGroupContext(chatKey) : "",
      status ? (ev) => status.onEvent(ev) : undefined,
    );
    // Collapse the status before the answer lands, so the thread reads
    // status-receipt-then-answer rather than answer-then-a-stale-"working…".
    await status?.finish(!result.isError);
    react(
      chatKey,
      ref,
      result.isError ? config.reactions.error : config.reactions.done,
    );
    // A group reply is just an answer in a conversation — the cwd/session/auto
    // banner is workspace bookkeeping and means nothing to the other people in
    // the room, so it stays on the 1:1 surface.
    if (group) {
      await sendReply(chatKey, `${result.isError ? "⚠ " : ""}${result.text}`);
    } else {
      const chat = getChat(chatKey);
      const prefix = replyPrefix(chat.cwd, chat.sessionId, chatMode(chatKey));
      await sendReply(
        chatKey,
        `${prefix}${result.isError ? " ⚠" : ""}\n${result.text}`,
      );
    }
  } catch (err) {
    await status?.replace(`⚠ gateway error`);
    react(chatKey, ref, config.reactions.error);
    await sendTo(chatKey, `⚠ gateway error: ${String(err)}`);
  } finally {
    statuses.delete(chatKey);
    queue.shift();
    if (queue.length) void drain(chatKey);
  }
}

/** The chat's permission stance, for the banner and !status. Empty string is
 * the default one: prompt over chat before anything mutates. */
function chatMode(chatKey: string): string {
  const chat = getChat(chatKey);
  if (chat.plan) return "plan";
  return chat.auto ? "auto" : "";
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

  switch (cmd) {
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
      const on = arg === "on";
      // Auto and plan are opposite answers to the same question, so setting
      // one clears the other (see !plan).
      updateChat(chatKey, { auto: on, plan: on ? false : chat.plan });
      return sendTo(
        chatKey,
        on
          ? "⚡ auto mode ON — tools run without asking" +
              (chat.plan ? " (plan off)" : "")
          : "✓ auto mode off — mutations will prompt",
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
      const killed = [
        stop(chatKey) ? "claude" : "",
        stopBash(chatKey) ? "bash" : "",
      ].filter(Boolean);
      return sendTo(
        chatKey,
        killed.length ? `✓ sent SIGTERM to ${killed.join(" + ")}` : "nothing running",
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
      return sendTo(
        chatKey,
        [
          `cwd: ${chat.cwd}`,
          `session: ${chat.sessionId ?? "(none)"}`,
          `model: ${chat.model ?? config.model} · effort: ${chat.effort ?? config.effort}`,
          isGroupChat(chatKey)
            ? `group: tools limited to ${config.groups.allowedTools}`
            : `mode: ${chatMode(chatKey) || "prompt on mutations"}`,
          `state: ${isRunning(chatKey) ? "running" : "idle"}${q ? `, ${q} queued` : ""}` +
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
          "!more shows the rest of a reply that was cut short\n" +
          "!bash <cmd> shell in the current cwd, no model · !usage [days] tokens\n" +
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
 * Takes several identifiers because one sender can arrive under more than one:
 * Signal sends an ACI (UUID) and only includes the phone number when the sender
 * shares it, which phone-number privacy makes the exception rather than the
 * rule. A match on ANY identifier admits the sender, so an entry in
 * values.local.yaml may be an E.164 number or a UUID, and listing both survives
 * either one going missing.
 */
export function matchesAllowlist(
  sender: string | string[],
  allowed: string[],
): boolean {
  const ids = (Array.isArray(sender) ? sender : [sender]).filter(Boolean);
  return ids.some((id) => allowed.includes(id));
}

export function isAllowed(
  sender: string | string[],
  allowed: string[],
): boolean {
  const ids = (Array.isArray(sender) ? sender : [sender]).filter(Boolean);
  if (matchesAllowlist(ids, allowed)) return true;
  if (droppedLog++ % 20 === 0)
    console.warn(
      `dropped message from non-allowlisted sender ${ids.join(" / ") || "(unidentified)"}`,
    );
  return false;
}
