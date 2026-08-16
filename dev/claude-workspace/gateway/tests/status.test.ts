import { expect, test } from "bun:test";
import type { StreamEvent } from "../src/claude.ts";
import {
  Status,
  formatElapsed,
  formatSpend,
  renderStatus,
  toolLabel,
} from "../src/status.ts";
import type { EditResult, MsgRef } from "../src/transport.ts";

// A transport that records instead of sending. The throttle runs at
// millisecond scale here so the tests exercise the real timer rather than a
// stubbed one, without waiting out the production three seconds.
const INTERVAL = 30;

function harness() {
  const sends: string[] = [];
  const edits: string[] = [];
  // Every edit target the status aimed at, in order — this is what proves the
  // chain follows Signal's revisions instead of re-aiming at the original.
  const targets: MsgRef[] = [];
  let revision = 1;
  const io = {
    async send(_c: string, text: string): Promise<MsgRef> {
      sends.push(text);
      return revision;
    },
    async edit(_c: string, ref: MsgRef, text: string): Promise<EditResult> {
      edits.push(text);
      targets.push(ref);
      // Stand in for a surface that makes each revision its own message.
      return { ok: true, next: ++revision };
    },
    supported: () => true,
  };
  return {
    sends,
    edits,
    targets,
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
  expect(h.edits[0]).toContain("Grep sendReaction");
  expect(h.edits[0]).toContain("3 tools");
});

test("the clock keeps ticking while a long tool call produces no events", async () => {
  // The failure this guards: one Bash call that runs for a minute emits
  // nothing, and the status used to sit frozen on whatever came before it.
  const h = harness();
  // A fake clock advancing a second per tick: the display counts in whole
  // seconds, so real time would make this a multi-second test for nothing.
  let clock = 0;
  const status = new Status("stdin:test", h.io, INTERVAL, () => (clock += 1000));
  await status.begin();
  h.edits.length = 0;

  status.onEvent(toolEvent("Bash", { command: "sleep 60" }));
  // Long enough for several ticks with no further events at all.
  await new Promise((r) => setTimeout(r, INTERVAL * 6));
  await status.finish(true);

  const ticks = h.edits.filter((e) => e.includes("Bash: sleep 60"));
  expect(ticks.length).toBeGreaterThan(1);
  // Every tick carries the elapsed clock, and it only goes up.
  const secs = ticks.map((e) => Number(e.match(/working… (\d+)s/)![1]));
  expect(secs).toEqual([...secs].sort((a, b) => a - b));
});

test("each edit targets the revision the last one produced", async () => {
  // Signal makes every revision its own message and stops accepting edits
  // aimed at a superseded one: re-targeting the original lands an edit or two
  // and is then ignored, which is what froze the status mid-run.
  const h = harness();
  let clock = 0;
  const status = new Status("stdin:test", h.io, INTERVAL, () => (clock += 5000));
  await status.begin();
  status.onEvent(toolEvent("Read", { file_path: "a.ts" }));
  await new Promise((r) => setTimeout(r, INTERVAL * 4));
  await status.finish(true);

  // First edit aims at the sent message; each later one at the newest revision.
  expect(h.targets.length).toBeGreaterThan(1);
  expect(h.targets).toEqual(h.targets.map((_, i) => i + 1));
});

test("progress edits stay inside the ten-revision budget, receipt included", async () => {
  // Signal drops edits past MAX_EDIT_COUNT (10), so a long run must not spend
  // the whole budget on the clock and have nothing left for the receipt.
  const h = harness();
  // A clock that leaps, so the doubling gap never holds a tick back — this
  // measures the budget, not the pacing.
  let clock = 0;
  const status = new Status("stdin:test", h.io, INTERVAL, () => (clock += 3_600_000));
  await status.begin();
  for (let i = 0; i < 40; i++) {
    status.onEvent(toolEvent("Read", { file_path: `f${i}.ts` }));
    await new Promise((r) => setTimeout(r, INTERVAL));
  }
  const beforeReceipt = h.edits.length;
  await status.finish(true);

  expect(beforeReceipt).toBe(9);
  expect(h.edits.length).toBe(10);
  expect(h.edits[9]).toStartWith("✓ done");
});

test("a tick that would render identical text is not sent", async () => {
  // Sub-second intervals redraw faster than the clock changes; that must not
  // spend an edit per tick on an unchanged line.
  const h = harness();
  const status = new Status("stdin:test", h.io, 5);
  await status.begin();
  h.edits.length = 0;
  status.onEvent(textEvent("thinking about it"));
  await new Promise((r) => setTimeout(r, 60));
  await status.finish(true);

  const working = h.edits.filter((e) => e.startsWith("⏺"));
  // ~12 ticks in that window, but at most a couple of distinct renders.
  expect(working.length).toBeLessThan(4);
  expect(new Set(working).size).toBe(working.length);
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

test("answering a prompt hands the status to a fresh message", async () => {
  // The run stops on a plan, the prompt and the reply land under the status,
  // and the ten revisions of the old message are spent waiting. Continuing to
  // edit it is progress nobody sees.
  const h = harness();
  let clock = 0;
  const status = new Status("stdin:test", h.io, INTERVAL, () => (clock += 3_600_000));
  await status.begin();
  for (let i = 0; i < 12; i++) {
    status.onEvent(toolEvent("Read", { file_path: `f${i}.ts` }));
    await new Promise((r) => setTimeout(r, INTERVAL));
  }
  expect(h.edits.length).toBe(9); // budget spent

  await status.restart();
  expect(h.edits[9]).toBe("⏺ answered — continuing below");
  expect(h.sends).toEqual(["⏺ working…", "⏺ working…"]);

  // A new message means a new budget, and the tool count carries over — it is
  // still the same run.
  status.onEvent(toolEvent("Bash", { command: "helm upgrade" }));
  await new Promise((r) => setTimeout(r, INTERVAL * 2));
  const resumed = h.edits[h.edits.length - 1];
  expect(resumed).toContain("Bash: helm upgrade");
  expect(resumed).toContain("13 tools");

  await status.finish(true);
  expect(h.edits[h.edits.length - 1]).toStartWith("✓ done · 13 tools");
});

test("restart on a status that never started is a no-op", async () => {
  const h = harness();
  const status = new Status("stdin:test", { ...h.io, supported: () => false }, INTERVAL);
  await status.begin();
  await status.restart();
  expect(h.sends).toEqual([]);
  expect(h.edits).toEqual([]);
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
