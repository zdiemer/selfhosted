import crypto from "node:crypto";
import { config } from "./config.ts";

/**
 * TOTP session unlock for 1:1 chats.
 *
 * The allowlist answers "is this the owner's account?". This answers "is the
 * owner actually holding the phone?" — the question a stolen device, a linked
 * device, or a recycled number all get the wrong answer to. It exists because
 * even the read-only baseline here is not harmless: Read and Grep over ~/code,
 * plus a pod that can reach 1Password, means a read-only session can exfiltrate
 * everything the cluster holds.
 *
 * Shape, chosen so the surface stays usable on a phone on airline wifi:
 *
 * - A chat is LOCKED after `idleMs` of silence, after any pod restart (state is
 *   in memory on purpose), and never longer than `maxMs` since the last unlock.
 * - Unlocking is one message: the six digits. Nothing else about the chat
 *   changes, and the sliding window means ordinary use asks about once a day.
 * - Escalation — `!bash`, `!auto` — needs a code entered within the last
 *   `freshMs` regardless of the window. Standing unlock is permission to talk;
 *   it is not permission to hand a shell to whoever picked the phone up.
 *
 * Off unless `GW_TOTP_SECRET` is set, so an unconfigured deploy behaves exactly
 * as it did before rather than locking the owner out of their own gateway.
 */

interface UnlockState {
  /** When the current unlock window opened (the absolute cap runs from here). */
  since: number;
  /** Last accepted code, for the escalation freshness check. */
  verifiedAt: number;
  /** Last activity, for the idle window. */
  touchedAt: number;
}

const unlocked = new Map<string, UnlockState>();
const failures = new Map<string, { count: number; until: number }>();

/**
 * Counter values already spent. Global rather than per chat: a code read over
 * someone's shoulder (or lifted from a notification preview) must not be
 * replayable into a second chat inside its 30-second life.
 */
const usedSteps = new Set<number>();

const STEP_SECONDS = 30;
const DIGITS = 6;
/** ±1 step, the usual allowance for clock drift between phone and pod. */
const SKEW_STEPS = 1;

export function unlockEnabled(): boolean {
  return Boolean(config.unlock.secret);
}

/** RFC 4648 base32, the format every authenticator app enrols from. */
function decodeBase32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = secret.replace(/[\s=-]/g, "").toUpperCase();
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const ch of clean) {
    const idx = alphabet.indexOf(ch);
    if (idx < 0) throw new Error(`invalid base32 character ${ch}`);
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238 / RFC 4226. No network, no clock but the system's own. */
function totpAt(key: Buffer, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeUInt32BE(Math.floor(step / 2 ** 32), 0);
  counter.writeUInt32BE(step >>> 0, 4);
  const hmac = crypto.createHmac("sha1", key).update(counter).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const bin =
    ((hmac[offset] & 0x7f) << 24) |
    (hmac[offset + 1] << 16) |
    (hmac[offset + 2] << 8) |
    hmac[offset + 3];
  return String(bin % 10 ** DIGITS).padStart(DIGITS, "0");
}

/** A message that is nothing but six digits — spaces tolerated, phones add them. */
export function looksLikeCode(body: string): boolean {
  return new RegExp(`^\\d{${DIGITS}}$`).test(body.replace(/[\s-]/g, ""));
}

/**
 * The step this code is valid for, or null. Constant-time compared, and a step
 * already spent is refused — without that, a code stays reusable for its whole
 * window plus the skew allowance.
 */
export function verifyCode(body: string, now = Date.now()): number | null {
  if (!config.unlock.secret) return null;
  const code = body.replace(/[\s-]/g, "");
  if (!looksLikeCode(code)) return null;
  let key: Buffer;
  try {
    key = decodeBase32(config.unlock.secret);
  } catch (err) {
    console.error(`unlock: GW_TOTP_SECRET is not base32 (${String(err)})`);
    return null;
  }
  const current = Math.floor(now / 1000 / STEP_SECONDS);
  for (let d = -SKEW_STEPS; d <= SKEW_STEPS; d++) {
    const step = current + d;
    const expected = totpAt(key, step);
    const a = Buffer.from(expected);
    const b = Buffer.from(code);
    if (a.length === b.length && crypto.timingSafeEqual(a, b)) {
      if (usedSteps.has(step)) return null;
      usedSteps.add(step);
      for (const spent of usedSteps)
        if (spent < current - SKEW_STEPS - 1) usedSteps.delete(spent);
      return step;
    }
  }
  return null;
}

function active(chatKey: string, now: number): UnlockState | undefined {
  const state = unlocked.get(chatKey);
  if (!state) return undefined;
  if (
    now - state.touchedAt > config.unlock.idleMs ||
    now - state.since > config.unlock.maxMs
  ) {
    unlocked.delete(chatKey);
    return undefined;
  }
  return state;
}

export function isUnlocked(chatKey: string, now = Date.now()): boolean {
  if (!unlockEnabled()) return true;
  return Boolean(active(chatKey, now));
}

/** Slide the idle window. Called for every message the chat is allowed to send. */
export function touch(chatKey: string, now = Date.now()): void {
  const state = active(chatKey, now);
  if (state) state.touchedAt = now;
}

/**
 * Was a code entered recently enough to authorise an escalation? Deliberately
 * not satisfied by a long-standing unlock: the window says the owner was here
 * at some point today, this says they are here now.
 */
export function isFresh(chatKey: string, now = Date.now()): boolean {
  if (!unlockEnabled()) return true;
  const state = active(chatKey, now);
  return Boolean(state && now - state.verifiedAt <= config.unlock.freshMs);
}

export function lock(chatKey: string): void {
  unlocked.delete(chatKey);
}

/** Remaining lockout in ms after too many wrong codes, or 0. */
function lockoutRemaining(chatKey: string, now: number): number {
  const f = failures.get(chatKey);
  if (!f || f.until <= now) return 0;
  return f.until - now;
}

function recordFailure(chatKey: string, now: number): void {
  const f = failures.get(chatKey) ?? { count: 0, until: 0 };
  f.count += 1;
  if (f.count >= config.unlock.maxAttempts) {
    f.count = 0;
    f.until = now + config.unlock.lockoutMs;
  }
  failures.set(chatKey, f);
}

export type AttemptResult =
  | { status: "unlocked"; reply: string }
  | { status: "rejected"; reply: string };

/**
 * Handle a message from a locked chat. Everything that is not a valid code is
 * answered with the same prompt — including a wrong code, so a guess learns
 * nothing beyond "not that one", which the lockout then bounds.
 */
export function attempt(
  chatKey: string,
  body: string,
  now = Date.now(),
): AttemptResult {
  const wait = lockoutRemaining(chatKey, now);
  if (wait > 0)
    return {
      status: "rejected",
      reply: `🔒 too many wrong codes — locked out for ${Math.ceil(wait / 60_000)}m`,
    };

  if (!looksLikeCode(body))
    return {
      status: "rejected",
      reply: "🔒 locked — send your 6-digit code to unlock this chat",
    };

  if (verifyCode(body, now) === null) {
    recordFailure(chatKey, now);
    return { status: "rejected", reply: "🔒 wrong code" };
  }

  failures.delete(chatKey);
  unlocked.set(chatKey, { since: now, verifiedAt: now, touchedAt: now });
  const hours = Math.round(config.unlock.idleMs / 3_600_000);
  return {
    status: "unlocked",
    reply:
      `🔓 unlocked — stays open while you're chatting, ${hours}h idle relocks it.\n` +
      "If this wasn't you, !lock now.",
  };
}

/**
 * Re-verify inside an already-unlocked chat, so `!bash`/`!auto` can be made
 * fresh without locking and unlocking around them.
 */
export function refresh(chatKey: string, now = Date.now()): boolean {
  const state = active(chatKey, now);
  if (!state) return false;
  state.verifiedAt = now;
  state.touchedAt = now;
  return true;
}

/** One line for `!status`. */
export function unlockSummary(chatKey: string, now = Date.now()): string {
  if (!unlockEnabled()) return "unlock: off (no TOTP secret configured)";
  const state = active(chatKey, now);
  if (!state) return "unlock: 🔒 locked";
  const idleLeft = config.unlock.idleMs - (now - state.touchedAt);
  const capLeft = config.unlock.maxMs - (now - state.since);
  const left = Math.min(idleLeft, capLeft);
  return (
    `unlock: 🔓 open (~${Math.max(1, Math.round(left / 60_000))}m left)` +
    (isFresh(chatKey, now) ? ", code fresh" : ", code stale (!bash/!auto need one)")
  );
}

/** Test seam: the code an authenticator would show right now. */
export function currentCode(now = Date.now()): string {
  return totpAt(
    decodeBase32(config.unlock.secret),
    Math.floor(now / 1000 / STEP_SECONDS),
  );
}
