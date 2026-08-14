import { beforeEach, describe, expect, test } from "bun:test";
import { config } from "../src/config.ts";

// The gateway reads the secret from the env at import time; tests set it on the
// live config object instead, so this file works whatever order bun loads in.
config.unlock.secret = "JBSWY3DPEHPK3PXP";
config.unlock.idleMs = 60 * 60_000;
config.unlock.maxMs = 4 * 60 * 60_000;
config.unlock.freshMs = 5 * 60_000;
config.unlock.maxAttempts = 3;
config.unlock.lockoutMs = 10 * 60_000;

const {
  attempt,
  currentCode,
  isFresh,
  isUnlocked,
  lock,
  looksLikeCode,
  refresh,
  touch,
  unlockEnabled,
  verifyCode,
} = await import("../src/unlock.ts");

// A fixed instant, so a step boundary can never make this suite flaky.
const T0 = 1_780_000_000_000;
let chat = 0;
let key = "";

beforeEach(() => {
  key = `signal:test-${++chat}`;
});

describe("code recognition", () => {
  test("six digits, spaces and dashes tolerated", () => {
    expect(looksLikeCode("123456")).toBe(true);
    expect(looksLikeCode("123 456")).toBe(true);
    expect(looksLikeCode("123-456")).toBe(true);
    expect(looksLikeCode("12345")).toBe(false);
    expect(looksLikeCode("1234567")).toBe(false);
    expect(looksLikeCode("deploy 123456")).toBe(false);
  });
});

describe("verifyCode", () => {
  test("accepts the code an authenticator shows now", () => {
    expect(verifyCode(currentCode(T0), T0)).not.toBeNull();
  });

  test("rejects a wrong code", () => {
    const wrong = currentCode(T0) === "000000" ? "111111" : "000000";
    expect(verifyCode(wrong, T0)).toBeNull();
  });

  test("accepts one step of drift either way, not two", () => {
    const base = T0 + 10 * 60_000;
    expect(verifyCode(currentCode(base - 30_000), base)).not.toBeNull();
    expect(verifyCode(currentCode(base + 30_000), base)).not.toBeNull();
    expect(verifyCode(currentCode(base + 90_000), base)).toBeNull();
  });

  test("a code cannot be spent twice", () => {
    const at = T0 + 20 * 60_000;
    const code = currentCode(at);
    expect(verifyCode(code, at)).not.toBeNull();
    // Same code, still inside its window — a shoulder-surfed code is dead.
    expect(verifyCode(code, at + 5_000)).toBeNull();
  });
});

describe("session window", () => {
  test("locked until a code arrives", () => {
    const at = T0 + 30 * 60_000;
    expect(isUnlocked(key, at)).toBe(false);
    expect(attempt(key, "hello", at).status).toBe("rejected");
    expect(isUnlocked(key, at)).toBe(false);
    expect(attempt(key, currentCode(at), at).status).toBe("unlocked");
    expect(isUnlocked(key, at)).toBe(true);
  });

  test("idle silence relocks; activity slides the window", () => {
    const at = T0 + 60 * 60_000;
    attempt(key, currentCode(at), at);
    const nearlyIdle = at + config.unlock.idleMs - 60_000;
    expect(isUnlocked(key, nearlyIdle)).toBe(true);
    touch(key, nearlyIdle);
    // Would have lapsed without the touch above.
    expect(isUnlocked(key, at + config.unlock.idleMs + 60_000)).toBe(true);
    expect(isUnlocked(key, nearlyIdle + config.unlock.idleMs + 60_000)).toBe(
      false,
    );
  });

  test("the absolute cap relocks a chat that never goes quiet", () => {
    const at = T0 + 120 * 60_000;
    attempt(key, currentCode(at), at);
    for (let t = at; t < at + config.unlock.maxMs; t += 30 * 60_000)
      touch(key, t);
    expect(isUnlocked(key, at + config.unlock.maxMs + 1)).toBe(false);
  });

  test("!lock takes effect immediately", () => {
    const at = T0 + 180 * 60_000;
    attempt(key, currentCode(at), at);
    lock(key);
    expect(isUnlocked(key, at)).toBe(false);
  });
});

describe("escalation freshness", () => {
  test("fresh right after a code, stale later, refreshable", () => {
    const at = T0 + 240 * 60_000;
    attempt(key, currentCode(at), at);
    expect(isFresh(key, at + 60_000)).toBe(true);
    const stale = at + config.unlock.freshMs + 60_000;
    expect(isFresh(key, stale)).toBe(false);
    // Still unlocked, just not fresh — the session survives, the grant doesn't.
    expect(isUnlocked(key, stale)).toBe(true);
    expect(refresh(key, stale)).toBe(true);
    expect(isFresh(key, stale)).toBe(true);
  });

  test("a locked chat is never fresh", () => {
    expect(isFresh(key, T0)).toBe(false);
  });
});

describe("brute force", () => {
  test("wrong codes buy a cooldown, and the cooldown outlasts a good code", () => {
    const at = T0 + 300 * 60_000;
    for (let i = 0; i < config.unlock.maxAttempts; i++)
      expect(attempt(key, "000000", at).status).toBe("rejected");
    const result = attempt(key, currentCode(at), at);
    expect(result.status).toBe("rejected");
    expect(result.reply).toContain("locked out");
    // And it lifts.
    const after = at + config.unlock.lockoutMs + 1000;
    expect(attempt(key, currentCode(after), after).status).toBe("unlocked");
  });
});

describe("unconfigured", () => {
  test("no secret means the gate is inert, not shut", () => {
    const secret = config.unlock.secret;
    config.unlock.secret = "";
    try {
      expect(unlockEnabled()).toBe(false);
      expect(isUnlocked("signal:anyone", T0)).toBe(true);
      expect(isFresh("signal:anyone", T0)).toBe(true);
    } finally {
      config.unlock.secret = secret;
    }
  });
});
