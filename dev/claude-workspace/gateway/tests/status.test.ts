import { expect, test } from "bun:test";
import type { StreamEvent } from "../src/claude.ts";
import {
  Status,
  formatElapsed,
  formatSpend,
  renderStatus,
  toolLabel,
} from "../src/status.ts";
import type { MsgRef } from "../src/transport.ts";

// A transport that records instead of sending. The throttle runs at
// millisecond scale here so the tests exercise the real timer rather than a
// stubbed one, without waiting out the production three seconds.
const INTERVAL = 30;

function harness() {
  const sends: string[] = [];
  const edits: string[] = [];
  const io = {
    async send(_c: string, text: string): Promise<MsgRef> {
      sends.push(text);
      return 1;
    },
    async edit(_c: string, _r: MsgRef, text: string): Promise<boolean> {
      edits.push(text);
      return true;
    },
    supported: () => true,
  };
  return {
    sends,
    edits,
    io,
    status: new Status("stdin:test", io, INTERVAL),
  };
}

const toolEvent = (name: string, input: Record<string, unknown>): StreamEvent => ({
  type: "assistant",
  message: { content: [{ type: "tool_use", name, input }] },
});

const textEvent = (text: string): StreamEvent => ({
  type: "assistant",
  message: { content: [{ type: "text", text }] },
});

/** Wait past one throttle window, plus slack for the edit itself. */
const settle = (): Promise<void> =>
  new Promise((r) => setTimeout(r, INTERVAL * 3));

test("tool labels are short and human", () => {
  // Long absolute paths are the norm here and unreadable on a phone.
  expect(
    toolLabel("Read", { file_path: "/home/node/code/selfhosted/src/router.ts" }),
  ).toBe("Read src/router.ts");
  expect(toolLabel("Bash", { command: "helm upgrade --install x" })).toBe(
    "Bash: helm upgrade --install x",
  );
  expect(toolLabel("WebFetch", { url: "https://example.com/a/b" })).toBe(
    "Fetch example.com",
  );
  // MCP tools arrive fully qualified; the approval relay is worth naming
  // because the run is blocked on the person reading the status.
  expect(toolLabel("mcp__gw__approve", {})).toBe("waiting for your approval");
  // An unknown tool shows its own name rather than vanishing.
  expect(toolLabel("SomethingNew", {})).toBe("SomethingNew");
});

test("elapsed reads as a duration, not a number of seconds", () => {
  expect(formatElapsed(4_400)).toBe("4s");
  expect(formatElapsed(102_000)).toBe("1m42s");
});

test("status shows the newest action and the first line of narration", () => {
  expect(
    renderStatus({ action: "Read a.ts", note: "looking", tools: 3, elapsedMs: 5000 }),
  ).toBe("⏺ working… 5s · 3 tools\nRead a.ts\nlooking");
  // Nothing has happened yet: no empty lines where an action would go.
  expect(renderStatus({ action: "", note: "", tools: 0, elapsedMs: 1000 })).toBe(
    "⏺ working… 1s",
  );
});

test("edits are throttled and coalesced, and the last state still lands", async () => {
  const h = harness();
  await h.status.begin();
  expect(h.sends).toEqual(["⏺ working…"]);

  // Three tools in quick succession inside one throttle window.
  h.status.onEvent(toolEvent("Read", { file_path: "src/a.ts" }));
  h.status.onEvent(toolEvent("Read", { file_path: "src/b.ts" }));
  h.status.onEvent(toolEvent("Grep", { pattern: "sendReaction" }));
  expect(h.edits).toEqual([]); // nothing yet — still inside the window

  await settle();
  // One edit for three events, showing the newest.
  expect(h.edits.length).toBe(1);
  expect(h.edits[0]).toContain("Grep sendReaction");
  expect(h.edits[0]).toContain("3 tools");
});

test("an unchanged render is not re-sent", async () => {
  const h = harness();
  await h.status.begin();
  h.status.onEvent(textEvent("thinking about it"));
  await settle();
  const after = h.edits.length;
  expect(after).toBe(1);

  // Same event again, no time passing: identical text, so no edit.
  h.status.onEvent(textEvent("thinking about it"));
  await settle();
  expect(h.edits.length).toBe(after);
});

test("finish collapses to a one-line receipt and stops updating", async () => {
  const h = harness();
  await h.status.begin();
  h.status.onEvent(toolEvent("Bash", { command: "ls" }));
  await h.status.finish(true);
  // Elapsed is real wall-clock here; formatElapsed is pinned separately above.
  expect(h.edits[h.edits.length - 1]).toMatch(/^✓ done · 1 tool · \d+s$/);

  // A late event after the run is over must not resurrect the status.
  const settled = h.edits.length;
  h.status.onEvent(toolEvent("Read", { file_path: "x.ts" }));
  await settle();
  expect(h.edits.length).toBe(settled);
});

test("a failed run says so", async () => {
  const h = harness();
  await h.status.begin();
  await h.status.finish(false);
  expect(h.edits[h.edits.length - 1]).toMatch(/^⚠ failed · 0 tools · \d+s$/);
});

test("a surface that cannot edit gets no status message at all", async () => {
  const h = harness();
  const status = new Status("stdin:test", { ...h.io, supported: () => false }, INTERVAL);
  await status.begin();
  status.onEvent(toolEvent("Read", { file_path: "a.ts" }));
  await status.finish(true);
  expect(h.sends).toEqual([]);
  expect(h.edits).toEqual([]);
});

test("the receipt reports what the run cost", async () => {
  const h = harness();
  await h.status.begin();
  h.status.onEvent({
    type: "result",
    usage: {
      input_tokens: 1_200,
      output_tokens: 800,
      cache_read_input_tokens: 36_000,
    },
    total_cost_usd: 0.2149,
  });
  await h.status.finish(true);
  // Cache reads count: the question is "was that expensive", and they are.
  expect(h.edits[h.edits.length - 1]).toMatch(/· 38k tok · \$0\.21$/);
});

test("spend is omitted when it isn't known", () => {
  // A run killed before its result event, or a subscription that doesn't
  // price one — absent, not zero.
  expect(formatSpend(0, 0)).toBe("");
  expect(formatSpend(940, 0)).toBe(" · 940 tok");
  expect(formatSpend(0, 0.004)).toBe(" · $0.004");
});
