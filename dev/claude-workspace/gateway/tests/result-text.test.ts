import { expect, test } from "bun:test";
import { assistantText, pickResultText, type StreamEvent } from "../src/claude.ts";

const assistant = (
  blocks: NonNullable<NonNullable<StreamEvent["message"]>["content"]>,
): StreamEvent => ({ type: "assistant", message: { content: blocks } });

test("assistantText joins text blocks and skips tool_use", () => {
  const ev = assistant([
    { type: "text", text: "checking the build" },
    { type: "tool_use", name: "Bash", input: {} },
    { type: "text", text: "still going" },
  ]);
  expect(assistantText(ev)).toBe("checking the build\nstill going");
});

test("assistantText is empty for tool-only and non-assistant events", () => {
  expect(assistantText(assistant([{ type: "tool_use", name: "Bash" }]))).toBe("");
  expect(assistantText({ type: "result", result: "done" })).toBe("");
});

test("pickResultText prefers a non-empty result", () => {
  expect(pickResultText("the answer", "earlier words")).toBe("the answer");
});

test("empty result falls back to the last assistant text", () => {
  // A turn that ends on a tool call (scheduled wake-up, background handoff)
  // reports result: "" — the reply must carry the words written before it.
  expect(pickResultText("", "build started, will report back")).toBe(
    "build started, will report back",
  );
  expect(pickResultText(undefined, "build started")).toBe("build started");
  expect(pickResultText("   ", "build started")).toBe("build started");
});

test("nothing anywhere still says so instead of sending a bare banner", () => {
  expect(pickResultText("", "")).toBe("(no result text)");
});
