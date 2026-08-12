import { isGroupChat } from "./chat.ts";
import { runningChats, stop } from "./claude.ts";
import { config } from "./config.ts";
import { activeStatus } from "./router.ts";
import { allChats, lastBootNotice, recordBootNotice, updateChat } from "./state.ts";
import { onTransportReady, sendTo } from "./transport.ts";

// Redeploying this chart is a `Recreate` rollout: the old pod is torn down
// before the new one pulls its image, so any run in flight simply stops, and
// the chat is left waiting on a reply that will never come. This says so —
// once on the way down, once on the way back up.

const DOWN = "⏳ gateway restarting — back shortly";
const UP = "✓ gateway back up";
const INTERRUPTED =
  "⚠ gateway restarted and your last run was cut off partway. " +
  'Say "continue" to pick it up from where it stopped.';

/** Chats worth telling. Groups are excluded: a room does not need the bot's
 * deployment lifecycle, and nobody in it is waiting on a run. */
function recentChats(prefix?: string): { chatKey: string; inFlight: boolean }[] {
  const cutoff = Date.now() - config.restartNotice.withinMs;
  return allChats()
    .filter(({ chatKey }) => !isGroupChat(chatKey))
    .filter(({ chatKey }) => !prefix || chatKey.startsWith(`${prefix}:`))
    .filter(({ chat }) => Date.parse(chat.updatedAt) >= cutoff)
    .map(({ chatKey, chat }) => ({ chatKey, inFlight: Boolean(chat.inFlight) }));
}

/**
 * Announce on each surface as it comes up. The dedupe decision is taken once,
 * at startup, before any transport connects — taking it per-transport would let
 * the first announcement suppress the second.
 */
export function announceRestart(): void {
  if (!config.restartNotice.enabled) return;
  const since = Date.now() - lastBootNotice();
  if (since < config.restartNotice.dedupeMs) {
    console.log(
      `restart notice: skipped, last one was ${Math.round(since / 1000)}s ago`,
    );
    return;
  }
  onTransportReady((prefix) => {
    const chats = recentChats(prefix);
    // Record only when someone is actually told. Stamping it on every boot
    // would let a restart nobody was around for silence the next one.
    if (!chats.length) return;
    recordBootNotice();

    for (const { chatKey, inFlight } of chats) {
      if (inFlight) {
        // The session id was persisted at run START (claude.ts), so the partial
        // transcript on the PVC is still reachable and "continue" really does
        // resume it. Clear the flag either way — it describes the dead process.
        updateChat(chatKey, { inFlight: false });
        void sendTo(chatKey, INTERRUPTED);
      } else {
        void sendTo(chatKey, UP);
      }
    }
  });
}

/**
 * Replaces the bare `process.exit(0)` SIGTERM handler. Strictly time-boxed:
 * the pod's terminationGracePeriodSeconds is the real ceiling and being
 * SIGKILLed mid-send is a normal outcome here, not a failure to handle.
 */
export function installShutdownHandler(): void {
  let shuttingDown = false;

  const handle = async (signal: string): Promise<void> => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`${signal}: shutting down`);

    const chats = runningChats();
    if (!config.restartNotice.enabled || !chats.length) process.exit(0);

    const hard = setTimeout(
      () => process.exit(0),
      config.restartNotice.shutdownGraceMs,
    );
    // Don't let the deadline itself be the reason we linger.
    hard.unref?.();

    await Promise.allSettled(
      chats.flatMap((chatKey) => [
        activeStatus(chatKey)?.replace(DOWN) ?? Promise.resolve(),
        sendTo(chatKey, DOWN),
      ]),
    );
    // After the word goes out, not before: killing first makes drain() race us
    // with an "claude exited null" reply on the way out the door.
    for (const chatKey of chats) stop(chatKey);
    process.exit(0);
  };

  process.on("SIGTERM", () => void handle("SIGTERM"));
  process.on("SIGINT", () => void handle("SIGINT"));
}
