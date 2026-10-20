// CLI-managed agents cannot survive their parent. Preserve their identity and
// outcome so a restart doesn't erase the evidence needed to resume from a
// checkpoint. Durable shell jobs are managed separately by jobs.ts.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { config } from "./config.ts";
import { deliveryKey } from "./chat.ts";
import type { BackgroundTask, StreamEvent } from "./claude.ts";
import { processIdentity, readJson, sameProcess, writeJson } from "./jobs-store.ts";

export interface NativeTask extends BackgroundTask {
  id: string;
  chatKey: string;
  sessionId: string;
  pid: number;
  identity?: string;
  state: string;
  updatedAt: number;
  outputFile?: string;
  cwd?: string;
  session?: string;
  startedAt?: number;
  lastTool?: string;
  summary?: string;
  tokens?: number;
  toolUses?: number;
  durationMs?: number;
}
const root = () => path.join(config.stateDir, "native-tasks");
function file(session: string, task: string): string {
  return path.join(root(), createHash("sha256").update(`${session}:${task}`).digest("hex") + ".json");
}
function save(task: NativeTask): void {
  fs.mkdirSync(root(), { recursive: true, mode: 0o700 });
  writeJson(file(task.sessionId, task.id), task);
}
function records(): NativeTask[] {
  if (!fs.existsSync(root())) return [];
  return fs.readdirSync(root()).filter((name) => name.endsWith(".json"))
    .flatMap((name) => {
      const task = readJson<NativeTask>(path.join(root(), name));
      return task ? [task] : [];
    });
}
export function trackNativeTasks(
  chatKey: string, sessionId: string, pid: number, ev: StreamEvent,
  origin?: { cwd: string; session: string },
): void {
  if (ev.type !== "system") return;
  if (ev.subtype === "background_tasks_changed") {
    const present = new Set((ev.tasks ?? []).map((task) => task.task_id));
    for (const old of records()) {
      if (old.chatKey === chatKey && old.pid === pid && old.sessionId === sessionId &&
          old.state === "running" && !present.has(old.id)) {
        save({ ...old, state: "awaiting-report", updatedAt: Date.now() });
      }
    }
    for (const task of ev.tasks ?? []) {
      if (!task.task_id) continue;
      const previous = readJson<NativeTask>(file(sessionId, task.task_id));
      save({ ...previous, ...task, ...origin, id: task.task_id, chatKey, sessionId, pid,
        startedAt: previous?.startedAt ?? Date.now(),
        identity: processIdentity(pid), state: "running", updatedAt: Date.now() });
    }
  } else if (["task_notification", "task_started", "task_progress"].includes(ev.subtype ?? "") && ev.task_id) {
    const previous = readJson<NativeTask>(file(sessionId, ev.task_id));
    save({ ...previous, ...origin, id: ev.task_id, task_id: ev.task_id, chatKey, sessionId, pid,
      startedAt: previous?.startedAt ?? Date.now(),
      description: ev.description ?? previous?.description,
      task_type: ev.task_type ?? previous?.task_type,
      lastTool: ev.last_tool_name ?? previous?.lastTool,
      summary: ev.summary ?? previous?.summary,
      tokens: ev.usage?.total_tokens ?? previous?.tokens,
      toolUses: ev.usage?.tool_uses ?? previous?.toolUses,
      durationMs: ev.usage?.duration_ms ?? previous?.durationMs,
      identity: processIdentity(pid),
      state: ev.subtype === "task_notification" ? ev.status ?? "reported" : "running",
      updatedAt: Date.now(),
      outputFile: ev.output_file ?? previous?.outputFile });
  }
}
export function interruptNativeTasks(chatKey: string, pid: number): number {
  let count = 0;
  for (const task of records()) {
    if (task.chatKey !== chatKey || task.pid !== pid ||
        !["running", "awaiting-report"].includes(task.state)) continue;
    save({ ...task, state: "interrupted", updatedAt: Date.now() });
    count++;
  }
  return count;
}
export function nativeTasksFor(chatKey: string): NativeTask[] {
  return records().filter((task) => deliveryKey(task.chatKey) === deliveryKey(chatKey))
    .map((task) => {
      if (["running", "awaiting-report"].includes(task.state) && !sameProcess(task.pid, task.identity)) {
        task = { ...task, state: "interrupted", updatedAt: Date.now() };
        save(task);
      }
      return task;
    }).sort((a, b) => b.updatedAt - a.updatedAt);
}
export function describeNativeTask(task: NativeTask): string {
  const seconds = Math.max(0, Math.round((task.durationMs ??
    ((isActiveTask(task) ? Date.now() : task.updatedAt) - (task.startedAt ?? task.updatedAt))) / 1000));
  return `@${taskAlias(task)} · ${task.state} · ${task.description ?? task.task_type ?? "CLI task"}` +
    ` · ${seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m`}` +
    (task.lastTool && isActiveTask(task) ? ` · ${task.lastTool}` : "") +
    (task.toolUses != null ? ` · ${task.toolUses} tools` : "") +
    (task.tokens != null ? ` · ${Math.round(task.tokens / 1000)}k tokens` : "");
}
export function taskAlias(task: NativeTask): string {
  return createHash("sha256").update(`${task.sessionId}:${task.id}`).digest("hex").slice(0, 6);
}
export function isActiveTask(task: NativeTask): boolean {
  return ["running", "awaiting-report"].includes(task.state);
}
export function findNativeTask(chatKey: string, alias: string): NativeTask {
  const value = alias.replace(/^@/, "");
  const matches = nativeTasksFor(chatKey).filter((task) => task.id === value || taskAlias(task).startsWith(value));
  if (!value || matches.length !== 1) throw new Error("task ID is missing, unknown, or ambiguous; use !tasks");
  return matches[0];
}
export function taskDetails(task: NativeTask): string {
  return [describeNativeTask(task), `task ID: ${task.id}`,
    `conversation: ${task.session ?? "main"} · ${task.sessionId}`,
    task.summary, task.outputFile ? `output: ${task.outputFile}` : "",
    "Use !task @" + taskAlias(task) + " cancel|resume to act in its originating conversation."]
    .filter(Boolean).join("\n");
}
export function taskActionPrompt(task: NativeTask, action: "cancel" | "resume"): string {
  return `The user requested ${action} for this CLI-managed task:\n${taskDetails(task)}\n` +
    (action === "cancel"
      ? `Stop only task ${task.id} using the task/agent tool. Confirm its outcome; do not stop unrelated tasks.`
      : "Inspect this task's transcript, output and checkpoint. Check whether it is already running before doing anything. " +
        "Resume its verified remaining work using its agent/transcript ID where supported; do not blindly replay completed mutations. " +
        "If its state cannot be restored, explain what can be recovered before starting replacement work.");
}
