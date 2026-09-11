import { hasLiveWakeup } from "./claude.ts";
import { allChats, getChat, updateChat } from "./state.ts";

// The gateway's half of ScheduleWakeup. The tool's own timer lives inside the
// headless `claude -p` child, and that process exits the moment its turn ends —
// so a wake-up armed there never fires (the original "monitors work, delays
// don't" bug). claude.ts spots the confirmed call in the stream; this module
// owns the timer in the process that actually outlives the run, and fires the
// scheduled prompt as a fresh run through the router's ordinary queue.
//
// The pending wake-up is persisted in chat state so a redeploy postpones it
// rather than erasing it; rearmWakeups() picks it back up per surface as each
// transport connects. An overdue one fires immediately.

export interface PendingWakeup {
  at: number;
  prompt: string;
  /** Pinned cwd/session, carried over when the wake-up was armed by a
   * gateway-scheduled run (claude.ts RunOverrides) — its follow-up must land
   * in the same thread, not whatever the chat points at by then. */
  run?: {
    cwd?: string;
    session?: string;
    fresh?: boolean;
    model?: string;
    effort?: string;
  };
  /** Schedule name, for the preamble the follow-up run is handed. */
  schedName?: string;
}

/** Runs the wake-up prompt as a fresh headless run. Registered by router.ts at
 * import time — the same registration shape as the transports, and for the
 * same reason: importing the router from here would be a cycle. */
type Runner = (chatKey: string, wakeup: PendingWakeup) => void;

let runner: Runner = () => {};
const timers = new Map<string, ReturnType<typeof setTimeout>>();

export function setWakeupRunner(r: Runner): void {
  runner = r;
}

export function armWakeup(chatKey: string, wakeup: PendingWakeup): void {
  updateChat(chatKey, { wakeup });
  schedule(chatKey, wakeup);
}

/** Clear the pending wake-up, if any. True if there was one to clear — the
 * caller (!stop) reports what it actually killed. */
export function cancelWakeup(chatKey: string): boolean {
  const timer = timers.get(chatKey);
  if (timer) clearTimeout(timer);
  timers.delete(chatKey);
  if (!getChat(chatKey).wakeup) return false;
  updateChat(chatKey, { wakeup: undefined });
  return true;
}

export function pendingWakeup(chatKey: string): PendingWakeup | undefined {
  return getChat(chatKey).wakeup;
}

/** Re-arm persisted wake-ups for one surface as it connects. Called through
 * onTransportReady, because firing earlier would send the reply into a socket
 * that isn't open yet. */
export function rearmWakeups(prefix: string): void {
  for (const { chatKey, chat } of allChats()) {
    if (!chat.wakeup || !chatKey.startsWith(`${prefix}:`)) continue;
    schedule(chatKey, chat.wakeup);
  }
}

function schedule(chatKey: string, wakeup: PendingWakeup): void {
  const old = timers.get(chatKey);
  if (old) clearTimeout(old);
  const timer = setTimeout(
    () => fire(chatKey),
    Math.max(0, wakeup.at - Date.now()),
  );
  // A pending wake-up must never be the reason the process won't exit: it is
  // on the PVC, and the next boot re-arms it.
  timer.unref?.();
  timers.set(chatKey, timer);
}

/** How long to keep deferring to a live child's own timer. It fires late (an
 * observed 60s wake-up took ~100s), so this is a re-check cadence, not a
 * deadline — each check finds either the state consumed (the child fired it,
 * onSelfWake) or the child gone (fire it ourselves). */
const SELF_FIRE_RECHECK_MS = 90_000;

function fire(chatKey: string): void {
  timers.delete(chatKey);
  const wakeup = getChat(chatKey).wakeup;
  if (!wakeup) return; // cancelled between arming and firing
  // The child that armed this wake-up is still alive (parked on background
  // work), and its own harness timer fires it in-process with the loop's full
  // context — injecting the prompt from here as well would run it twice.
  // Check back instead: if the child fired it, onSelfWake has cleared the
  // state by then; if the child died first, the persisted copy is still here.
  if (hasLiveWakeup(chatKey)) {
    const timer = setTimeout(() => fire(chatKey), SELF_FIRE_RECHECK_MS);
    timer.unref?.();
    timers.set(chatKey, timer);
    return;
  }
  // Cleared before the run rather than after: the run itself is what arms the
  // next wake-up (or doesn't — which is how a loop ends).
  updateChat(chatKey, { wakeup: undefined });
  runner(chatKey, wakeup);
}
