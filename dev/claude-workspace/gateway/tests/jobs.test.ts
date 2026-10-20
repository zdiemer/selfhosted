import { afterAll, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { startJob, cancelJob, jobStatus, jobLog, findJob, pollJobs, reconcileJob, retryJobReport } from "../src/jobs.ts";
import { jobDir, readJson, writeJson, sameProcess } from "../src/jobs-store.ts";
import { config } from "../src/config.ts";

const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "gateway-jobs-"));
// Local tests may launch only in their scratch root.
config.cwdRoots.push(cwd);
afterAll(() => { config.cwdRoots.splice(config.cwdRoots.indexOf(cwd), 1); });
let sequence = 0;
const input = (command: string) => ({
  chatKey: `jobs:case-${++sequence}`, sessionId: "origin-transcript", session: "work",
  cwd, title: "test job", command,
});
async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for job");
    await Bun.sleep(20);
  }
}

test("detached job persists output, exit status and originating transcript", async () => {
  const job = startJob(input("printf 'saved output'; exit 3"));
  await until(() => jobStatus(job).state === "failed");
  expect(jobStatus(job).exitCode).toBe(3);
  expect(jobLog(job)).toContain("saved output");
  expect(findJob(job.chatKey, job.id.slice(0, 8)).sessionId).toBe("origin-transcript");
  expect(() => findJob("jobs:someone-else", job.id)).toThrow();
  expect(() => jobDir("../escape")).toThrow();
});

test("cancelling a job stops its tracked command group", async () => {
  const job = startJob(input("sleep 100 & wait"));
  await until(() => Boolean(jobStatus(job).commandPid));
  const before = jobStatus(job);
  cancelJob(job);
  await until(() => jobStatus(job).state === "cancelled");
  expect(sameProcess(before.commandPid, before.commandIdentity)).toBe(false);
});

test("a missing worker is interrupted, never rerun", () => {
  const id = "a".repeat(24);
  const job = { ...input("never execute"), id, createdAt: Date.now() - 60_000 };
  fs.mkdirSync(jobDir(id), { recursive: true });
  writeJson(path.join(jobDir(id), "job.json"), job);
  writeJson(path.join(jobDir(id), "status.json"), { state: "running", pid: process.pid, identity: "wrong-boot" });
  reconcileJob(job);
  expect(jobStatus(job).state).toBe("interrupted");
});

test("a worker survives its launching process exiting", async () => {
  const spec = { ...input("sleep 0.3; printf survived"), chatKey: "survival:owner" };
  const script = `import {startJob} from ${JSON.stringify(path.resolve(import.meta.dir, "../src/jobs.ts"))};
    console.log(JSON.stringify(startJob(${JSON.stringify(spec)})));
    process.exit(0);`;
  const launcher = Bun.spawn(["bun", "-e", script], {
    env: { ...process.env, GW_STATE_DIR: config.stateDir, GW_CWD_ROOTS: cwd },
    stdout: "pipe", stderr: "pipe",
  });
  const output = await new Response(launcher.stdout).text();
  expect(await launcher.exited).toBe(0);
  const job = JSON.parse(output);
  await until(() => jobStatus(job).state === "succeeded");
  expect(jobLog(job)).toContain("survived");
});

test("completion survives send failure and is acknowledged only after conversation handling", async () => {
  const job = startJob({ ...input("printf complete"), chatKey: "delivery:owner" });
  await until(() => jobStatus(job).state === "succeeded");
  let attempts = 0;
  let handled = 0;
  await pollJobs("delivery", async () => { attempts++; throw new Error("offline"); },
    async () => { handled++; return true; });
  expect(handled).toBe(0);
  const receiptFile = path.join(jobDir(job.id), "delivery.json");
  expect(readJson(receiptFile)).toBeUndefined();
  await pollJobs("delivery", async () => { attempts++; }, async (origin, prompt) => {
    handled++;
    expect(origin.sessionId).toBe("origin-transcript");
    expect(prompt).toContain("complete");
    return false;
  });
  expect(readJson<{ handled: boolean }>(receiptFile)?.handled).toBe(false);
  writeJson(receiptFile, { notified: true, handled: false });
  await pollJobs("delivery", async () => { attempts++; }, async () => { handled++; return true; });
  await pollJobs("delivery", async () => { attempts++; }, async () => { handled++; return true; });
  expect(attempts).toBe(2);
  expect(handled).toBe(2);
  expect(readJson<{ handled: boolean }>(receiptFile)?.handled).toBe(true);
});

test("failed model reports stop retrying after three attempts and can be explicitly retried", async () => {
  const job = startJob({ ...input("true"), chatKey: "report-retry:owner" });
  await until(() => jobStatus(job).state === "succeeded");
  const file = path.join(jobDir(job.id), "delivery.json");
  let attempts = 0;
  const notices: string[] = [];
  const notify = async (_job: unknown, text: string) => { notices.push(text); };
  for (let i = 0; i < 4; i++) {
    await pollJobs("report-retry", notify, async () => { attempts++; return false; });
    writeJson(file, { ...readJson<object>(file), retryAt: 0 });
  }
  expect(attempts).toBe(3);
  expect(notices.length).toBe(2);
  expect(notices[1]).toContain("report to retry");
  retryJobReport(job);
  await pollJobs("report-retry", notify, async () => { attempts++; return true; });
  expect(attempts).toBe(4);
  expect(readJson<{ handled: boolean }>(file)?.handled).toBe(true);
});
