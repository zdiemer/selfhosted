import { config } from "./config.ts";

// Group-chat plumbing. Groups are a different trust surface from a 1:1: the
// allowlist controls who can *drive* the shell, but every member reads whatever
// claude prints. So groups get their own tool set, their own rate limit, and no
// permission prompts at all (see claude.ts / approvals.ts).

/** Chat keys are `signal:<sender>`, `signal:g:<groupId>`, or `wa:<jid>`. */
export function isGroupChat(chatKey: string): boolean {
  return chatKey.startsWith("signal:g:") || chatKey.endsWith("@g.us");
}

// Everything said in a group becomes context for the next run, not just the
// messages addressed to the bot — a question like "what did we decide?" is
// meaningless without the surrounding conversation. In memory only: this is
// ambient chatter, and losing it on restart is the safe direction.
const buffers = new Map<string, string[]>();

export function recordGroupMessage(
  chatKey: string,
  sender: string,
  text: string,
): void {
  const buf = buffers.get(chatKey) ?? [];
  buf.push(`${sender}: ${text}`);
  // Keep the newest N lines; an active group would otherwise grow unbounded
  // between mentions and blow out the prompt.
  while (buf.length > config.groups.contextLines) buf.shift();
  buffers.set(chatKey, buf);
}

/** Drain the buffered chatter into a prompt preamble. Empty string if none. */
export function takeGroupContext(chatKey: string): string {
  const buf = buffers.get(chatKey);
  if (!buf?.length) return "";
  buffers.delete(chatKey);
  return (
    "Recent messages in this group chat, for context. Only the final message " +
    "is addressed to you; the rest is conversation between other people:\n" +
    buf.join("\n")
  );
}

// Per-group sliding-window rate limit. This is a personal Claude subscription:
// a group is the one surface where people other than the owner can spend it.
const runs = new Map<string, number[]>();

export function groupRateAllows(chatKey: string): boolean {
  const now = Date.now();
  const cutoff = now - config.groups.rateWindowMs;
  const recent = (runs.get(chatKey) ?? []).filter((t) => t > cutoff);
  if (recent.length >= config.groups.rateLimit) {
    runs.set(chatKey, recent);
    return false;
  }
  recent.push(now);
  runs.set(chatKey, recent);
  return true;
}

export function groupRateResetMinutes(chatKey: string): number {
  const oldest = (runs.get(chatKey) ?? [])[0];
  if (!oldest) return 0;
  return Math.max(
    1,
    Math.ceil((oldest + config.groups.rateWindowMs - Date.now()) / 60_000),
  );
}
