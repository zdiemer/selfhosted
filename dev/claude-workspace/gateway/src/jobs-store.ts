import fs from "node:fs";
import path from "node:path";
import { config } from "./config.ts";

export interface Job {
  id: string;
  chatKey: string;
  sessionId: string;
  session: string;
  cwd: string;
  title: string;
  command: string;
  createdAt: number;
  /** A file the job maintains with progress and instructions for resuming. */
  checkpoint?: string;
}
export interface JobStatus {
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  pid?: number;
  identity?: string;
  commandPid?: number;
  commandIdentity?: string;
  startedAt?: number;
  endedAt?: number;
  exitCode?: number | null;
  detail?: string;
}
export function jobDir(id: string): string {
  if (!/^[a-f0-9]{24}$/.test(id)) throw new Error("invalid job ID");
  return path.join(config.stateDir, "jobs", id);
}
export function writeJson(file: string, value: unknown): void {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value), { mode: 0o600 });
  fs.renameSync(tmp, file);
}
export function readJson<T>(file: string): T | undefined {
  try { return JSON.parse(fs.readFileSync(file, "utf8")) as T; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
}
export function jobStatus(job: Job): JobStatus {
  return readJson<JobStatus>(path.join(jobDir(job.id), "status.json")) ??
    { ...readJson<JobStatus>(path.join(jobDir(job.id), "worker.json")), state: "queued" };
}
export function terminal(status: JobStatus): boolean {
  return status.state !== "queued" && status.state !== "running";
}
/** Linux boot ID + process start tick: a reused PID is never our worker. */
export function processIdentity(pid: number): string | undefined {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    if (fields[0] === "Z") return;
    return fs.readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() + ":" + fields[19];
  } catch { return; }
}
export function sameProcess(pid?: number, identity?: string): boolean {
  return Boolean(pid && identity && processIdentity(pid) === identity);
}
export function allJobs(): Job[] {
  const root = path.join(config.stateDir, "jobs");
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root).filter((id) => /^[a-f0-9]{24}$/.test(id)).flatMap((id) => {
    const job = readJson<Job>(path.join(jobDir(id), "job.json"));
    return job ? [job] : [];
  }).sort((a, b) => b.createdAt - a.createdAt);
}
export function jobLog(job: Job, bytes = 6000): string {
  const file = path.join(jobDir(job.id), "output.log");
  if (!fs.existsSync(file)) return "";
  const fd = fs.openSync(file, "r");
  try {
    const size = fs.fstatSync(fd).size;
    const buf = Buffer.alloc(Math.min(bytes, size));
    fs.readSync(fd, buf, 0, buf.length, Math.max(0, size - buf.length));
    return buf.toString("utf8");
  } finally { fs.closeSync(fd); }
}
