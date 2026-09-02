import { expect, test } from "bun:test";
import { takeNotifyMarker } from "../src/router.ts";

// A gateway schedule is silent unless the run marks its text for delivery.
// These are the shapes the trading agent actually produced on 2026-09-01,
// every one of which reached the phone under the old empty-text-only rule.

test("a routine sign-off is not a notification", () => {
  const { notify, text } = takeNotifyMarker(
    "Evening crypto run done, no action, journaled and pushed. Nothing that needs you.",
  );
  expect(notify).toBe(false);
  expect(text).toBe(
    "Evening crypto run done, no action, journaled and pushed. Nothing that needs you.",
  );
});

test("the marker opts in and is taken out of the text", () => {
  const { notify, text } = takeNotifyMarker(
    "[[notify]]\nGLD stop hit at $385 — position closed, -$12.40.",
  );
  expect(notify).toBe(true);
  expect(text).toBe("GLD stop hit at $385 — position closed, -$12.40.");
});

test("the marker is found anywhere in the message, not just the first line", () => {
  const { notify, text } = takeNotifyMarker(
    "Weekly report\n\nAccount $446.66, up 0.2%.\n\n[[notify]]",
  );
  expect(notify).toBe(true);
  expect(text).toBe("Weekly report\n\nAccount $446.66, up 0.2%.");
});

test("only a line of its own counts — a mention in prose does not send", () => {
  const { notify, text } = takeNotifyMarker(
    "I considered writing [[notify]] here but there is nothing to say.",
  );
  expect(notify).toBe(false);
  expect(text).toContain("[[notify]]");
});

test("the hole the marker leaves is collapsed", () => {
  const { text } = takeNotifyMarker("First line.\n\n[[notify]]\n\nSecond line.");
  expect(text).toBe("First line.\n\nSecond line.");
});

test("a marker with nothing else leaves no text for the router to send", () => {
  const { notify, text } = takeNotifyMarker("  [[NOTIFY]]  ");
  expect(notify).toBe(true);
  expect(text).toBe("");
});
