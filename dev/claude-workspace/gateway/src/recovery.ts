import { randomUUID } from "node:crypto";
import { config } from "./config.ts";
import { allChats, getChat, updateChat, threadOf, sessionName } from "./state.ts";
import { isGroupChat } from "./chat.ts";
import type { RunOverrides } from "./claude.ts";

export interface Recovery {
  id: string;
  agent: "claude" | "codex" | "gemini" | "muse";
  cwd: string;
  session: string;
  sessionId?: string;
  prompt: string;
  startedAt: number;
  attempts: number;
  pendingUserReply: boolean;
}
let shuttingDown = false;
export function beginShutdown(): void { shuttingDown = true; }
export function isShuttingDown(): boolean { return shuttingDown; }

export function beginRecovery(
  chatKey: string, input: Omit<Recovery, "id" | "startedAt" | "attempts">,
  prior?: Recovery,
): Recovery {
  const record: Recovery = { ...input, id: prior?.id ?? randomUUID(),
    startedAt: prior?.startedAt ?? Date.now(), attempts: prior?.attempts ?? 0 };
  updateChat(chatKey, { recovery: record });
  return record;
}
export function patchRecovery(chatKey: string, id: string, patch: Partial<Recovery>): void {
  const current = getChat(chatKey).recovery;
  if (current?.id === id) updateChat(chatKey, { recovery: { ...current, ...patch } });
}
export function clearRecovery(chatKey: string, id?: string): void {
  if (id && getChat(chatKey).recovery?.id !== id) return;
  updateChat(chatKey, { recovery: undefined });
}
export function recoveryPrompt(record: Recovery, tasks = ""): string {
  return "The workspace container restarted during this conversation. Resume the interrupted work. " +
    "First inspect the transcript, current files, job/task status and checkpoints. " +
    "Do not repeat completed mutations or restart a worker that is still alive. " +
    "Continue from verified progress, respecting the existing permission mode. " +
    (record.pendingUserReply
      ? `The request still awaiting a reply was:\n${record.prompt}`
      : "The previous reply was already delivered; continue only the outstanding background work.") +
    (tasks ? `\nRecorded tasks:\n${tasks}` : "");
}

/** A persisted run is claimed once per process; queued recovery survives a
 * second crash. Notification dedupe never controls recovery eligibility. */
const claimed = new Set<string>();
export function isRecoveryClaimed(id: string): boolean { return claimed.has(id); }
export function takeRecoveries(prefix: string): { chatKey: string; record: Recovery; run: RunOverrides }[] {
  if (!config.autoResume.enabled) return [];
  const result = [];
  for (const { chatKey, chat } of allChats()) {
    if (!chatKey.startsWith(`${prefix}:`) || isGroupChat(chatKey)) continue;
    let record = chat.recovery;
    // Migration for the first upgrade from the inFlight-only gateway.
    if (!record && chat.inFlight) {
      const agent = (["claude", "codex", "gemini", "muse"].includes(chat.backend ?? "") ? chat.backend : "claude") as Recovery["agent"];
      const thread = threadOf(chat, agent);
      if (!thread.sessionId) continue;
      record = { id: randomUUID(), agent, cwd: chat.cwd, session: sessionName(thread),
        sessionId: thread.sessionId, prompt: "Continue the interrupted request from the transcript.",
        startedAt: Date.parse(chat.updatedAt), attempts: 0, pendingUserReply: true };
    }
    if (!record || claimed.has(record.id) || record.attempts >= config.autoResume.maxAttempts ||
        Date.now() - record.startedAt > config.autoResume.maxAgeMs) continue;
    claimed.add(record.id);
    record = { ...record, attempts: record.attempts + 1 };
    updateChat(chatKey, { recovery: record });
    result.push({ chatKey, record, run: {
      agent: record.agent, cwd: record.cwd, session: record.session,
      resumeId: record.sessionId, fresh: !record.sessionId,
    } });
  }
  return result;
}
