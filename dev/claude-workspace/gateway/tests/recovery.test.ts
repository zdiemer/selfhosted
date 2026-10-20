import { afterEach, expect, test } from "bun:test";
import { beginRecovery, clearRecovery, patchRecovery, takeRecoveries, recoveryPrompt } from "../src/recovery.ts";
import { getChat, updateChat } from "../src/state.ts";
import { config } from "../src/config.ts";

const saved = { ...config.autoResume };
afterEach(() => Object.assign(config.autoResume, saved));
const input = {
  agent: "claude" as const, cwd: "/tmp", session: "original", sessionId: "old-transcript",
  prompt: "Finish the test suite", pendingUserReply: true,
};
test("recovery pins the original backend, directory and transcript despite a later switch", () => {
  const key = "restart-pin:owner";
  beginRecovery(key, input);
  updateChat(key, { backend: "codex", cwd: "/new", sessionId: "new-transcript", session: "new" });
  const [recovery] = takeRecoveries("restart-pin");
  expect(recovery.run).toMatchObject({ agent: "claude", cwd: "/tmp", resumeId: "old-transcript", session: "original" });
  expect(recovery.record.attempts).toBe(1);
  expect(takeRecoveries("restart-pin")).toEqual([]);
  expect(getChat(key).sessionId).toBe("new-transcript");
});
test("notice suppression is unrelated to automatic recovery", () => {
  const before = config.restartNotice.enabled;
  config.restartNotice.enabled = false;
  try {
    beginRecovery("restart-no-notice:owner", input);
    expect(takeRecoveries("restart-no-notice").length).toBe(1);
  } finally { config.restartNotice.enabled = before; }
});
test("explicit stop/clear prevents stale events from resurrecting recovery", () => {
  const record = beginRecovery("restart-stop:owner", input);
  clearRecovery("restart-stop:owner");
  patchRecovery("restart-stop:owner", record.id, { sessionId: "late-event" });
  expect(getChat("restart-stop:owner").recovery).toBeUndefined();
  expect(takeRecoveries("restart-stop")).toEqual([]);
});
test("old records and repeated crash attempts do not create restart loops", () => {
  const old = beginRecovery("restart-limits:old", input);
  patchRecovery("restart-limits:old", old.id, { startedAt: Date.now() - config.autoResume.maxAgeMs - 1000 });
  const loop = beginRecovery("restart-limits:loop", input);
  patchRecovery("restart-limits:loop", loop.id, { attempts: config.autoResume.maxAttempts });
  expect(takeRecoveries("restart-limits")).toEqual([]);
});
test("an interrupted first turn without a session ID can recover its saved request", () => {
  beginRecovery("restart-fresh:owner", { ...input, sessionId: undefined });
  const [recovery] = takeRecoveries("restart-fresh");
  expect(recovery.run.fresh).toBe(true);
  expect(recovery.run.resumeId).toBeUndefined();
  expect(recoveryPrompt(recovery.record)).toContain("Finish the test suite");
});
test("legacy inFlight state migrates while idle conversations stay idle", () => {
  updateChat("restart-legacy:active", { inFlight: true, cwd: "/tmp", sessionId: "legacy" });
  updateChat("restart-legacy:idle", { inFlight: false, cwd: "/tmp", sessionId: "idle" });
  expect(takeRecoveries("restart-legacy").map((r) => r.run.resumeId)).toEqual(["legacy"]);
});
test("already-delivered replies are not requested again on a parked-run recovery", () => {
  const record = beginRecovery("restart-parked:owner", { ...input, pendingUserReply: false });
  const prompt = recoveryPrompt(record, "task 123 interrupted");
  expect(prompt).toContain("previous reply was already delivered");
  expect(prompt).toContain("task 123 interrupted");
  expect(prompt).not.toContain("Finish the test suite");
});
