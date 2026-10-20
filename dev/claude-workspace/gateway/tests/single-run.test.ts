import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { isRunning, isWaiting, runClaude } from "../src/claude.ts";
import { config } from "../src/config.ts";
import { updateChat } from "../src/state.ts";

// One claude per chat. On 2026-10-19 a chat ended up with two live CLIs
// resuming the same session, and both acted on the same wake-up (both merged
// one branch and pushed). These run the real runClaude against a fake CLI
// (fixtures/fake-claude-bin/claude) to pin the two ways that can happen.

const BIN = path.join(import.meta.dir, "fixtures", "fake-claude-bin");
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "gw-single-run-"));
const startLog = path.join(scratch, "starts.log");
const savedPath = process.env.PATH;
const savedBackground = { ...config.background };
let n = 0;

beforeAll(() => {
  process.env.PATH = `${BIN}:${savedPath}`;
  process.env.FAKE_CLAUDE_LOG = startLog;
});
afterAll(() => {
  process.env.PATH = savedPath;
  delete process.env.FAKE_CLAUDE_LOG;
});
afterEach(() => {
  delete process.env.FAKE_CLAUDE_PARK;
  delete process.env.FAKE_CLAUDE_LINGER;
  delete process.env.FAKE_CLAUDE_STALE;
  delete process.env.FAKE_CLAUDE_CRASH_CONTINUATION;
  Object.assign(config.background, savedBackground);
});

function chat(): string {
  const key = `test:single-run-${++n}`;
  updateChat(key, { cwd: scratch });
  return key;
}

const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

function startedPids(): number[] {
  return fs
    .readFileSync(startLog, "utf8")
    .trim()
    .split("\n")
    .map((l) => Number(l.split(" ")[1]));
}

async function until(cond: () => boolean, ms = 5000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error("timed out waiting");
    await Bun.sleep(20);
  }
}

test("a CLI that stays up after its input ends is stopped", async () => {
  process.env.FAKE_CLAUDE_LINGER = "1";
  Object.assign(config.background, { exitGraceMs: 200 });
  const key = chat();
  const result = await runClaude(key, "hello");
  // runClaude only resolves on close, so getting here at all is the fix: the
  // lingering child was stopped rather than left registered forever.
  expect(result.text).toBe("ok");
  expect(isRunning(key)).toBe(false);
  expect(alive(startedPids().at(-1)!)).toBe(false);
});

test("a second run for a chat stops the first and stays registered", async () => {
  process.env.FAKE_CLAUDE_PARK = "1";
  const key = chat();
  const first = runClaude(key, "one");
  await until(() => isWaiting(key));
  const firstPid = startedPids().at(-1)!;

  // drain() would hand this to the parked run; calling runClaude directly is
  // the path that used to leave two CLIs live on one chat.
  delete process.env.FAKE_CLAUDE_PARK;
  const second = runClaude(key, "two");
  await first;
  expect(alive(firstPid)).toBe(false);
  // The first run's close must not have unregistered the second.
  expect(isRunning(key)).toBe(true);
  await second;
  expect(isRunning(key)).toBe(false);
});

test("a stale empty result before prompt acknowledgment neither replies nor closes stdin", async () => {
  process.env.FAKE_CLAUDE_STALE = "1";
  Object.assign(config.background, { exitGraceMs: 30 });
  const replies: string[] = [];
  const result = await runClaude(chat(), "answer this message", "", {
    onTurn: async (result) => { replies.push(result.text); },
  });
  expect(replies).toEqual(["ok"]);
  expect(result.aborted).not.toBe(true);
});

test("a continuation crash cannot return an earlier successful result", async () => {
  process.env.FAKE_CLAUDE_PARK = "1";
  process.env.FAKE_CLAUDE_CRASH_CONTINUATION = "1";
  const result = await runClaude(chat(), "work");
  expect(result.isError).toBe(true);
  expect(result.aborted).toBe(true);
  expect(result.text).toContain("interrupted");
});

test("a delivery failure settles the run instead of leaving it hung", async () => {
  const result = await runClaude(chat(), "hello", "", {
    onTurn: async () => { throw new Error("transport offline"); },
  });
  expect(result.isError).toBe(true);
  expect(result.text).toContain("transport offline");
});
