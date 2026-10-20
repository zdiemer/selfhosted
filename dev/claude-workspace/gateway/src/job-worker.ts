// Detached from Claude and the gateway process. Only this worker writes status
// while alive; the gateway reconciles it after a crash using process identity.
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import {
  type Job, type JobStatus, jobDir, processIdentity, readJson, writeJson,
} from "./jobs-store.ts";

const dir = jobDir(process.argv[2]);
const job = readJson<Job>(path.join(dir, "job.json"))!;
const status: JobStatus = {
  state: "running", pid: process.pid, identity: processIdentity(process.pid),
  startedAt: Date.now(),
};
const save = () => writeJson(path.join(dir, "status.json"), status);
save();
const log = fs.openSync(path.join(dir, "output.log"), "a", 0o600);
let logged = 0;
let stopping = false;
let finished = false;
const child = spawn("bash", ["-lc", job.command], {
  cwd: job.cwd, detached: true, stdio: ["ignore", "pipe", "pipe"],
  env: { ...process.env, GW_JOB_ID: job.id, GW_JOB_CHECKPOINT: job.checkpoint ?? "" },
});
status.commandPid = child.pid;
status.commandIdentity = child.pid ? processIdentity(child.pid) : undefined;
save();
const kill = (signal: NodeJS.Signals) => {
  if (!child.pid) return;
  try { process.kill(-child.pid, signal); } catch { /* already gone */ }
};
const output = (data: Buffer) => {
  const remaining = 10 * 1024 * 1024 - logged;
  if (remaining > 0) {
    const chunk = data.subarray(0, remaining);
    fs.writeSync(log, chunk);
    logged += chunk.length;
    if (logged === 10 * 1024 * 1024) fs.writeSync(log, "\n[log capped at 10 MiB]\n");
  }
};
child.stdout?.on("data", output);
child.stderr?.on("data", output);
const stop = () => {
  if (stopping || finished) return;
  stopping = true;
  kill("SIGTERM");
  setTimeout(() => { if (!finished) kill("SIGKILL"); }, 3000).unref();
};
const poll = setInterval(() => {
  if (fs.existsSync(path.join(dir, "cancel"))) stop();
}, 250);
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
function finish(code: number | null, error?: string): void {
  if (finished) return;
  finished = true;
  clearInterval(poll);
  status.state = stopping ? "cancelled" : code === 0 ? "succeeded" : "failed";
  status.exitCode = code;
  status.detail = error;
  status.endedAt = Date.now();
  save();
  fs.closeSync(log);
}
child.on("error", (error) => finish(null, error.message));
// A command must not daemonize grandchildren beyond its tracked lifetime.
child.on("exit", () => kill("SIGKILL"));
child.on("close", (code) => finish(code));
