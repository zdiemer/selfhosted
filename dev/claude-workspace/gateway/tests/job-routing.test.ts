import { afterAll, beforeAll, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import { handleJobCompletion } from "../src/router.ts";
import { getChat, updateChat } from "../src/state.ts";
import { registerTransport } from "../src/transport.ts";
import { isRunning, isWaiting, runClaude, stop } from "../src/claude.ts";
import { approvalSocketPath, config } from "../src/config.ts";
import { type Job, jobsFor, jobStatus, jobLog } from "../src/jobs.ts";
import { startApprovalServer, stopApprovalServer, hasPending, answerPending } from "../src/approvals.ts";

const originalPath = process.env.PATH;
const progress = config.progress.enabled;
const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "job-routing-"));
const messages: string[] = [];
beforeAll(async () => {
  process.env.PATH = `${path.join(import.meta.dir, "fixtures/fake-claude-bin")}:${originalPath}`;
  config.progress.enabled = true;
  registerTransport("job-route", {
    chunkLimit: 10000,
    async send(_chat, text) {
      messages.push(text);
      // Simulates a slow status send before the process is registered.
      if (text.includes("working")) await Bun.sleep(40);
      return messages.length;
    },
    async edit() {},
  });
  await startApprovalServer((_chat, text) => { messages.push(text); });
});
afterAll(() => {
  process.env.PATH = originalPath;
  config.progress.enabled = progress;
  stopApprovalServer();
});

function rpc(chatKey: string, toolName: string, input: unknown): Promise<any> {
  return new Promise((resolve, reject) => {
    const socket = net.connect(approvalSocketPath);
    let buffer = "";
    socket.on("connect", () => socket.write(JSON.stringify({ chatKey, toolName, input, operation: "job" }) + "\n"));
    socket.on("error", reject);
    socket.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.includes("\n")) {
        resolve(JSON.parse(buffer.slice(0, buffer.indexOf("\n"))));
        socket.end();
      }
    });
  });
}
async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 3000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await Bun.sleep(10);
  }
}
test("job RPC enforces group, plan and shell approvals; an allowed job outlives Claude", async () => {
  expect((await rpc("signal:g:room", "start", {})).error).toContain("groups");
  updateChat("job-route:plan", { plan: true });
  expect((await rpc("job-route:plan", "start", {})).error).toContain("plan mode");
  const key = "job-route:approval";
  updateChat(key, { cwd, auto: false });
  const roots = [...config.cwdRoots];
  config.cwdRoots.push(cwd);
  process.env.FAKE_CLAUDE_PARK = "1";
  const run = runClaude(key, "start");
  try {
    await until(() => isWaiting(key));
    const denied = rpc(key, "start", { title: "denied", command: "printf not-allowed" });
    await until(() => hasPending(key));
    answerPending(key, "2");
    expect((await denied).error).toContain("denied");
    expect(jobsFor(key)).toEqual([]);
    updateChat(key, { auto: true });
    const job = await rpc(key, "start", { title: "allowed", command: "sleep 0.2; printf survived-claude" }) as Job;
    expect(job.sessionId).toBe("fake-session");
    stop(key);
    await run;
    await until(() => jobStatus(job).state === "succeeded");
    expect(jobLog(job)).toContain("survived-claude");
  } finally {
    stop(key);
    await run;
    delete process.env.FAKE_CLAUDE_PARK;
    config.cwdRoots.splice(0, config.cwdRoots.length, ...roots);
  }
});

test("simultaneous completions serialize and resume the origin without repointing a switched chat", async () => {
  const chatKey = "job-route:owner";
  updateChat(chatKey, { cwd, sessionId: "selected-new-thread", verbosity: "normal" });
  const job: Job = {
    id: "b".repeat(24), chatKey, cwd, title: "build", command: "unused",
    sessionId: "original-job-thread", session: "old-work", createdAt: Date.now(),
  };
  const results = await Promise.all([
    handleJobCompletion(job, "first completion"),
    handleJobCompletion({ ...job, id: "c".repeat(24) }, "second completion"),
  ]);
  expect(results).toEqual([true, true]);
  expect(getChat(chatKey).sessionId).toBe("selected-new-thread");
  expect(messages.filter((text) => text.endsWith("\nok")).length).toBe(2);
  expect(messages.some((text) => text.includes("no result text") || text.includes("interrupted"))).toBe(false);
  const until = Date.now() + 1000;
  while (isRunning(chatKey) && Date.now() < until) await Bun.sleep(10);
  expect(isRunning(chatKey)).toBe(false);
});
