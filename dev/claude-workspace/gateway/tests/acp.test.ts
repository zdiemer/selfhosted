import { afterAll, beforeAll, expect, test } from "bun:test";
import path from "node:path";
import {
  answerPending,
  hasPending,
  startApprovalServer,
  stopApprovalServer,
} from "../src/approvals.ts";
import {
  acpSystemPrompt,
  makeAcpBackend,
  toStreamEvent,
} from "../src/agent/acp-backend.ts";
import { ACP_FOREGROUND, BACKGROUND_PARAGRAPH } from "../src/config.ts";

// Must match fixtures/fake-acp.ts. Not imported: loading the fixture would
// start its stdin reader inside the test process.
const REPLAYED = "OLD REPLY FROM HISTORY";
const NARRATION = "Let me look first.";
import type { StreamEvent } from "../src/claude.ts";
import { getChat, threadOf, updateChat } from "../src/state.ts";
import { registerTransport } from "../src/transport.ts";

// The fake agent stands in for codex-acp / gemini --acp / muse-acp. Everything
// below is the client's side of the protocol, which is the part this repo owns
// — the real adapters were verified by hand (see agent/acp.ts) and cannot run
// in a test: two of the three need a subscription login on the PVC.

const FIXTURE = path.join(import.meta.dir, "fixtures", "fake-acp.ts");

const prompts: { chatKey: string; text: string }[] = [];
const lastPrompt = () => prompts[prompts.length - 1]?.text ?? "";

let msgId = 0;
registerTransport("acp", {
  chunkLimit: 4000,
  async send() {
    return ++msgId;
  },
  refId: (ref) => String(ref),
});

beforeAll(async () => {
  await startApprovalServer((chatKey, text) => {
    prompts.push({ chatKey, text });
    return Promise.resolve(++msgId as unknown as number);
  });
});

afterAll(() => stopApprovalServer());

function backend(env: Record<string, string> = {}) {
  for (const [k, v] of Object.entries(env)) process.env[k] = v;
  return makeAcpBackend({
    id: "codex", // any id; the spec is what's under test
    label: "Fake",
    command: "bun",
    args: [FIXTURE],
    defaultModel: () => "fake-default",
  });
}

function clearEnv(...keys: string[]) {
  for (const k of keys) delete process.env[k];
}

// ---------------------------------------------------------------------------
// Normalisation — a pure function, and the reason status.ts needs no changes
// ---------------------------------------------------------------------------

test("an agent message chunk becomes an assistant text event", () => {
  const ev = toStreamEvent({
    sessionUpdate: "agent_message_chunk",
    content: { type: "text", text: "hi" },
  });
  expect(ev).toEqual({
    type: "assistant",
    message: { content: [{ type: "text", text: "hi" }] },
  });
});

test("a tool call becomes a tool_use keyed by its ACP id", () => {
  const ev = toStreamEvent({
    sessionUpdate: "tool_call",
    toolCallId: "tc-9",
    title: "Read(/etc/hosts)",
    kind: "read",
    rawInput: { path: "/etc/hosts" },
  }) as StreamEvent;
  const block = ev.message?.content?.[0];
  expect(block?.type).toBe("tool_use");
  expect(block?.id).toBe("tc-9");
  // The TITLE, not the coarse kind: "Read(/etc/hosts)" is what tells you what
  // is about to happen.
  expect(block?.name).toBe("Read(/etc/hosts)");
});

test("only a finished tool call produces a result event", () => {
  expect(
    toStreamEvent({ sessionUpdate: "tool_call_update", status: "in_progress" }),
  ).toBeUndefined();
  const done = toStreamEvent({
    sessionUpdate: "tool_call_update",
    toolCallId: "tc-9",
    status: "failed",
  }) as StreamEvent;
  expect(done.message?.content?.[0]).toEqual({
    type: "tool_result",
    tool_use_id: "tc-9",
    is_error: true,
  });
});

test("reasoning traces and other updates are dropped", () => {
  expect(
    toStreamEvent({
      sessionUpdate: "agent_thought_chunk",
      content: { type: "text", text: "thinking" },
    }),
  ).toBeUndefined();
  expect(toStreamEvent({ sessionUpdate: "current_mode_update" })).toBeUndefined();
});

// ---------------------------------------------------------------------------
// A run, end to end
// ---------------------------------------------------------------------------

test("a run handshakes, opens a session, and returns the reply", async () => {
  const chat = "acp:run1";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const events: StreamEvent[] = [];

  const res = await backend().run(chat, "do the thing", "", {
    onEvent: (ev) => events.push(ev),
  });

  expect(res.isError).toBe(false);
  expect(res.text).toContain("hello from fake");
  expect(res.sessionId).toBe("fake-new-1");
  // The session id is persisted under THIS backend's thread, not the top
  // level, so claude's thread is untouched.
  expect(threadOf(getChat(chat), "codex").sessionId).toBe("fake-new-1");
  expect(getChat(chat).sessionId).toBeUndefined();
  expect(events.some((e) => e.type === "result")).toBe(true);
});

test("the harness system prompt rides in front of the first message only", async () => {
  const chat = "acp:sysprompt";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const seen: string[] = [];
  const b = backend();

  // The fixture echoes the prompt it received back through the tool call's
  // rawInput, which is how we can see what was actually sent.
  const capture = (ev: StreamEvent) => {
    const input = ev.message?.content?.[0]?.input as
      | { prompt?: string }
      | undefined;
    if (input?.prompt) seen.push(input.prompt);
  };

  await b.run(chat, "first", "", { onEvent: capture });
  await b.run(chat, "second", "", { onEvent: capture });

  expect(seen[0]).toContain("first");
  expect(seen[0].length).toBeGreaterThan("first".length); // prompt prepended
  // Second turn resumes, so it carries the message alone.
  expect(seen[1]).toBe("second");
});

test("a resumed thread's replayed history is not part of the reply", async () => {
  const chat = "acp:replay";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const b = backend();
  await b.run(chat, "first", "", {});

  const events: StreamEvent[] = [];
  const res = await b.run(chat, "second", "", {
    onEvent: (ev) => events.push(ev),
  });

  expect(res.text).toContain("hello from fake");
  expect(res.text).not.toContain(REPLAYED);
  // Nor does the replay reach the status message as tool calls.
  expect(JSON.stringify(events)).not.toContain("Old tool");
});

test("the reply is the last message, not the narration before tool calls", async () => {
  const chat = "acp:narration";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const res = await backend().run(chat, "do it", "", {});
  expect(res.text).toContain("hello from fake");
  expect(res.text).not.toContain(NARRATION);
});

test("an ACP agent is told to wait, not promised a wake-up", () => {
  const p = acpSystemPrompt(`intro\n\n${BACKGROUND_PARAGRAPH}\n\noutro`);
  expect(p).not.toContain(BACKGROUND_PARAGRAPH);
  expect(p).toBe(`intro\n\n${ACP_FOREGROUND}\n\noutro`);
  // A custom prompt without the paragraph still gets the warning.
  expect(acpSystemPrompt("custom")).toBe(`custom\n\n${ACP_FOREGROUND}`);
});

test("a resume falls back to a new session when the agent has lost the thread", async () => {
  const chat = "acp:lostthread";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { sessionId: "long-gone" } },
  });
  const b = backend({ FAKE_LOAD_FAILS: "1" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_LOAD_FAILS");

  expect(res.isError).toBe(false);
  expect(res.sessionId).toBe("fake-new-1");
});

test("an agent that cannot load a session starts fresh instead of erroring", async () => {
  const chat = "acp:noload";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { sessionId: "whatever" } },
  });
  const b = backend({ FAKE_LOAD_SESSION: "0" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_LOAD_SESSION");

  expect(res.isError).toBe(false);
  expect(res.sessionId).toBe("fake-new-1");
});

test("a dead agent reports its stderr rather than hanging", async () => {
  const chat = "acp:dead";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const b = backend({ FAKE_EXIT_EARLY: "1" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_EXIT_EARLY");

  expect(res.isError).toBe(true);
  expect(res.aborted).toBe(true);
  expect(res.text).toContain("Fake failed");
});

// ---------------------------------------------------------------------------
// Permissions — the reason ACP was chosen over each CLI's own headless mode
// ---------------------------------------------------------------------------

test("a permission request reaches the chat as 1/2/3 and an allow is mapped back", async () => {
  const chat = "acp:perm";
  updateChat(chat, { cwd: process.cwd(), backends: undefined, auto: false });
  const b = backend({ FAKE_PERMISSION: "1" });

  const run = b.run(chat, "write a file", "", {});
  // Wait for the prompt to land, then answer it the way a phone would.
  for (let i = 0; i < 100 && !hasPending(chat); i++)
    await new Promise((r) => setTimeout(r, 20));
  expect(hasPending(chat)).toBe(true);
  // The prompt names the agent and the ACP tool title, not "Claude" and not a
  // raw JSON blob.
  expect(lastPrompt()).toContain("Write(/tmp/fake)");
  expect(answerPending(chat, "1")).toBe(true);

  const res = await run;
  clearEnv("FAKE_PERMISSION");
  // a1 is the allow_once option the fixture offered.
  expect(res.text).toContain("[permission:selected:a1]");
});

test("a deny is mapped to the agent's reject option", async () => {
  const chat = "acp:perm2";
  updateChat(chat, { cwd: process.cwd(), backends: undefined, auto: false });
  const b = backend({ FAKE_PERMISSION: "1" });

  const run = b.run(chat, "write a file", "", {});
  for (let i = 0; i < 100 && !hasPending(chat); i++)
    await new Promise((r) => setTimeout(r, 20));
  expect(answerPending(chat, "2")).toBe(true);

  const res = await run;
  clearEnv("FAKE_PERMISSION");
  expect(res.text).toContain("[permission:selected:r1]");
});

test("an agent offering unlabelled options still gets a usable answer", async () => {
  const chat = "acp:perm3";
  updateChat(chat, { cwd: process.cwd(), backends: undefined, auto: false });
  const b = backend({
    FAKE_PERMISSION: "1",
    // No `kind` at all — the fallback has to read the names.
    FAKE_PERMISSION_OPTS: JSON.stringify([
      { optionId: "x1", name: "Proceed" },
      { optionId: "x2", name: "Deny it" },
    ]),
  });

  const run = b.run(chat, "write a file", "", {});
  for (let i = 0; i < 100 && !hasPending(chat); i++)
    await new Promise((r) => setTimeout(r, 20));
  expect(answerPending(chat, "1")).toBe(true);

  const res = await run;
  clearEnv("FAKE_PERMISSION", "FAKE_PERMISSION_OPTS");
  expect(res.text).toContain("[permission:selected:x1]");
});

test("auto mode answers without troubling the phone", async () => {
  const chat = "acp:auto";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: undefined,
    auto: true,
    autoUntil: undefined,
    plan: false,
  });
  const before = prompts.length;
  const b = backend({ FAKE_PERMISSION: "1" });

  const res = await b.run(chat, "write a file", "", {});
  clearEnv("FAKE_PERMISSION");
  updateChat(chat, { auto: false });

  expect(res.text).toContain("[permission:selected:a1]");
  expect(prompts.length).toBe(before); // nothing was asked
});

test("a group chat is refused rather than asked", async () => {
  const chat = "acp:group@g.us";
  updateChat(chat, { cwd: process.cwd(), backends: undefined, auto: false });
  const before = prompts.length;
  const b = backend({ FAKE_PERMISSION: "1" });

  const res = await b.run(chat, "write a file", "", {});
  clearEnv("FAKE_PERMISSION");

  expect(res.text).toContain("[permission:selected:r1]");
  expect(prompts.length).toBe(before);
});

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

test("an ACP backend claims none of claude's flag-only extras", () => {
  const s = backend().supportsFor("acp:caps");
  expect(s.plan).toBe(false);
  expect(s.systemPrompt).toBe(false);
  expect(s.wakeup).toBe(false);
  expect(s.backgroundTasks).toBe(false);
  expect(s.usage).toBe(false);
  expect(s.projectMcp).toBe(false);
});

test("an agent we have not spoken to yet is given the benefit of the doubt", () => {
  // Refusing !model before we know whether it works would be its own small lie.
  const s = backend().supportsFor("acp:never-run");
  expect(s.model).toBe(true);
  expect(s.effort).toBe(true);
});

// ---------------------------------------------------------------------------
// Model + effort selection — the bug this round exists to fix
// ---------------------------------------------------------------------------

test("the resolved model is actually sent as a config option", async () => {
  const chat = "acp:model1";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { model: "fake-fast" } },
  });
  const res = await backend().run(chat, "hi", "", {});

  // The fixture echoes back what it ended up set to, so this asserts the
  // EFFECT rather than merely that a request was sent.
  expect(res.text).toContain("[model:fake-fast]");
  expect(res.text).toContain("sets:model=fake-fast");
});

test("a model already selected costs no round trip", async () => {
  const chat = "acp:model2";
  updateChat(chat, {
    cwd: process.cwd(),
    // fake-default is the fixture's currentValue already.
    backends: { codex: { model: "fake-default" } },
  });
  const res = await backend().run(chat, "hi", "", {});
  expect(res.text).toContain("[model:fake-default]");
  expect(res.text).toContain("sets:]"); // nothing was set
});

test("the chart default applies when the chat has not chosen", async () => {
  const chat = "acp:model3";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const b = makeAcpBackend({
    id: "codex",
    label: "Fake",
    command: "bun",
    args: [FIXTURE],
    defaultModel: () => "fake-fast",
  });
  const res = await b.run(chat, "hi", "", {});
  expect(res.text).toContain("[model:fake-fast]");
});

test("a schedule's pinned model beats the chat's", async () => {
  const chat = "acp:model4";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { model: "fake-default" } },
  });
  const res = await backend().run(chat, "hi", "", {}, { model: "fake-fast" });
  expect(res.text).toContain("[model:fake-fast]");
});

test("a model the agent does not offer fails the run loudly", async () => {
  const chat = "acp:model5";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { model: "not-a-real-model" } },
  });
  const res = await backend().run(chat, "hi", "", {});

  expect(res.isError).toBe(true);
  // Naming what IS available is the difference between a dead end and a fix.
  expect(res.text).toContain("not-a-real-model");
  expect(res.text).toContain("fake-fast");
});

test("an agent that rejects the selection fails the run rather than answering on another model", async () => {
  const chat = "acp:model6";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { model: "fake-fast" } },
  });
  const b = backend({ FAKE_SET_CONFIG_FAILS: "1" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_SET_CONFIG_FAILS");

  expect(res.isError).toBe(true);
  expect(res.text).toContain("invalid_model");
});

test("an empty catalogue means not-known-yet, not nothing-is-valid", async () => {
  // muse-acp advertises `model` with an empty option list until `muse login`
  // has refreshed it from the host. The value must still be attempted.
  const chat = "acp:model7";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { model: "whatever-muse-has" } },
  });
  const b = backend({ FAKE_EMPTY_MODELS: "1" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_EMPTY_MODELS");

  expect(res.isError).toBe(false);
  expect(res.text).toContain("sets:model=whatever-muse-has");
});

test("an agent advertising no config options runs anyway and says model is unavailable", async () => {
  const chat = "acp:model8";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const b = backend({ FAKE_NO_CONFIG: "1" });
  const res = await b.run(chat, "hi", "", {});
  clearEnv("FAKE_NO_CONFIG");

  expect(res.isError).toBe(false);
  // And now that we have seen a session, !model knows to refuse.
  expect(b.supportsFor(chat).model).toBe(false);
  expect(b.supportsFor(chat).effort).toBe(false);
});

test("effort is not Claude-only: it rides the thought_level option", async () => {
  const chat = "acp:effort1";
  updateChat(chat, { cwd: process.cwd(), backends: undefined, effort: "high" });
  const res = await backend().run(chat, "hi", "", {});
  updateChat(chat, { effort: undefined });

  expect(res.text).toContain("[effort:high]");
});

test("the agent's catalogue is readable between runs, for !model to list", async () => {
  const chat = "acp:choices";
  updateChat(chat, { cwd: process.cwd(), backends: undefined });
  const b = backend();
  expect(b.choicesFor(chat, "model").values).toEqual([]); // nothing seen yet

  await b.run(chat, "hi", "", {});

  const { values, current } = b.choicesFor(chat, "model");
  expect(values.map((v) => v.value)).toEqual(["fake-default", "fake-fast"]);
  expect(current).toBe("fake-default");
  expect(b.choicesFor(chat, "effort").values.map((v) => v.value)).toEqual([
    "low",
    "medium",
    "high",
  ]);
});

test("a resumed session picks up the catalogue too", async () => {
  const chat = "acp:choices2";
  updateChat(chat, {
    cwd: process.cwd(),
    backends: { codex: { sessionId: "fake-new-1" } },
  });
  const b = backend();
  await b.run(chat, "hi", "", {});
  // session/load published them, so a resumed thread can still switch model.
  expect(b.choicesFor(chat, "model").values.length).toBe(2);
});

test("no transcript means no resume-by-default and no health nudge", () => {
  const b = backend();
  expect(b.latestSessionId("/home/node/code")).toBeUndefined();
  expect(b.resumableBytes("/home/node/code", "any")).toBe(0);
});
