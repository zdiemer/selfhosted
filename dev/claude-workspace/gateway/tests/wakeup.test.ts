import { expect, test } from "bun:test";
import { WakeupTracker, type StreamEvent } from "../src/claude.ts";
import { updateChat, getChat } from "../src/state.ts";
import {
  armWakeup,
  cancelWakeup,
  pendingWakeup,
  rearmWakeups,
  setWakeupRunner,
} from "../src/wakeup.ts";

const NOW = 1_000_000_000_000;

const call = (id: string, input: Record<string, unknown>): StreamEvent => ({
  type: "assistant",
  message: {
    content: [{ type: "tool_use", name: "ScheduleWakeup", id, input }],
  },
});

const okResult = (id: string): StreamEvent => ({
  type: "user",
  message: { content: [{ type: "tool_result", tool_use_id: id }] },
});

const errResult = (id: string): StreamEvent => ({
  type: "user",
  message: { content: [{ type: "tool_result", tool_use_id: id, is_error: true }] },
});

function track(events: StreamEvent[]): ReturnType<WakeupTracker["take"]> {
  const t = new WakeupTracker(() => NOW);
  for (const ev of events) t.onEvent(ev);
  return t.take();
}

test("a confirmed call arms a wake-up, delay counted from the call", () => {
  const state = track([
    call("a", { delaySeconds: 300, prompt: "check the build" }),
    okResult("a"),
  ]);
  expect(state).toEqual({ at: NOW + 300_000, prompt: "check the build" });
});

test("an unanswered or denied call arms nothing", () => {
  // No tool_result at all — the stream ended first.
  expect(track([call("a", { delaySeconds: 300, prompt: "x" })])).toBeUndefined();
  // Denied by the permission relay (is_error) — the model was refused.
  expect(
    track([call("a", { delaySeconds: 300, prompt: "x" }), errResult("a")]),
  ).toBeUndefined();
});

test("delaySeconds clamps to [60, 3600] like the harness promises", () => {
  const low = track([call("a", { delaySeconds: 5, prompt: "x" }), okResult("a")]);
  expect(low).toEqual({ at: NOW + 60_000, prompt: "x" });
  const high = track([
    call("b", { delaySeconds: 90_000, prompt: "x" }),
    okResult("b"),
  ]);
  expect(high).toEqual({ at: NOW + 3_600_000, prompt: "x" });
});

test("a later confirmed call replaces the earlier one; stop cancels", () => {
  const replaced = track([
    call("a", { delaySeconds: 60, prompt: "first" }),
    okResult("a"),
    call("b", { delaySeconds: 120, prompt: "second" }),
    okResult("b"),
  ]);
  expect(replaced).toEqual({ at: NOW + 120_000, prompt: "second" });

  const stopped = track([
    call("a", { delaySeconds: 60, prompt: "first" }),
    okResult("a"),
    call("b", { stop: true }),
    okResult("b"),
  ]);
  expect(stopped).toBe("stop");
});

test("take() consumes: a turn that armed nothing reports nothing", () => {
  const t = new WakeupTracker(() => NOW);
  t.onEvent(call("a", { delaySeconds: 60, prompt: "x" }));
  t.onEvent(okResult("a"));
  expect(t.take()).toEqual({ at: NOW + 60_000, prompt: "x" });
  // The next turn made no call — it must not re-arm the previous one.
  expect(t.take()).toBeUndefined();
  // But the CHILD's live timer is not per-turn: it stands until fired/stopped.
  expect(t.pendingAt).toBe(NOW + 60_000);
  t.fired();
  expect(t.pendingAt).toBeNull();
});

test("pendingAt follows the child's timer: stop clears it", () => {
  const t = new WakeupTracker(() => NOW);
  t.onEvent(call("a", { delaySeconds: 60, prompt: "x" }));
  t.onEvent(okResult("a"));
  t.onEvent(call("b", { stop: true }));
  t.onEvent(okResult("b"));
  expect(t.pendingAt).toBeNull();
  expect(t.take()).toBe("stop");
});

test("malformed input (no prompt, bad delay) arms nothing", () => {
  expect(track([call("a", { delaySeconds: 60 }), okResult("a")])).toBeUndefined();
  expect(
    track([call("a", { delaySeconds: "60", prompt: "x" }), okResult("a")]),
  ).toBeUndefined();
  expect(
    track([call("a", { delaySeconds: 60, prompt: "  " }), okResult("a")]),
  ).toBeUndefined();
});

// ---------------------------------------------------------------------------
// The gateway-side timer (wakeup.ts)
// ---------------------------------------------------------------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test("armWakeup persists, fires the runner once, and clears state", async () => {
  const fired: [string, string][] = [];
  setWakeupRunner((chatKey, prompt) => fired.push([chatKey, prompt]));

  armWakeup("test:fire", { at: Date.now() + 20, prompt: "wake" });
  expect(pendingWakeup("test:fire")).toBeDefined();
  await sleep(80);
  expect(fired).toEqual([["test:fire", "wake"]]);
  // Cleared before the run: the run itself is what arms the next one.
  expect(pendingWakeup("test:fire")).toBeUndefined();
});

test("cancelWakeup clears the pending wake-up and reports it", async () => {
  const fired: string[] = [];
  setWakeupRunner((chatKey) => fired.push(chatKey));

  armWakeup("test:cancel", { at: Date.now() + 20, prompt: "wake" });
  expect(cancelWakeup("test:cancel")).toBe(true);
  expect(pendingWakeup("test:cancel")).toBeUndefined();
  expect(cancelWakeup("test:cancel")).toBe(false); // nothing left to cancel
  await sleep(60);
  expect(fired).toEqual([]);
});

test("rearming a new wake-up replaces the old timer, not doubles it", async () => {
  const fired: string[] = [];
  setWakeupRunner((_chatKey, prompt) => fired.push(prompt));

  armWakeup("test:replace", { at: Date.now() + 20, prompt: "first" });
  armWakeup("test:replace", { at: Date.now() + 40, prompt: "second" });
  await sleep(100);
  expect(fired).toEqual(["second"]);
});

test("rearmWakeups picks up persisted wake-ups for its surface only", async () => {
  const fired: string[] = [];
  setWakeupRunner((chatKey) => fired.push(chatKey));

  // Written straight to state, as if a previous process armed them and died.
  // Both overdue, so they fire as soon as they are re-armed.
  updateChat("test:boot", { wakeup: { at: Date.now() - 1000, prompt: "go" } });
  updateChat("other:boot", { wakeup: { at: Date.now() - 1000, prompt: "go" } });

  rearmWakeups("test");
  await sleep(50);
  expect(fired).toEqual(["test:boot"]);
  expect(getChat("other:boot").wakeup).toBeDefined(); // still parked

  rearmWakeups("other");
  await sleep(50);
  expect(fired).toEqual(["test:boot", "other:boot"]);
});
