import { config } from "../config.ts";
import { getChat } from "../state.ts";
import type { AgentBackend, AgentId } from "./backend.ts";
import { claudeBackend } from "./claude-backend.ts";
import { acpBackends } from "./providers.ts";

export type { AgentBackend, AgentId, AgentSupports } from "./backend.ts";
export { atCapacity } from "./slots.ts";

// The registry. claude is first and is the default for every chat that has
// never said otherwise — including every chat whose state was written before
// backends existed, whose `backend` is simply absent.
const BACKENDS: AgentBackend[] = [claudeBackend, ...acpBackends()];

const byId = new Map<AgentId, AgentBackend>(BACKENDS.map((b) => [b.id, b]));

export const DEFAULT_AGENT: AgentId = "claude";

export function allBackends(): AgentBackend[] {
  return BACKENDS;
}

export function backendById(id: string): AgentBackend | undefined {
  return byId.get(id as AgentId);
}

/** The backend this chat is pointed at. Falls back to claude for an unset or
 * unrecognised id — a state file naming a backend this build dropped must not
 * strand the chat. */
export function backendFor(chatKey: string): AgentBackend {
  return backendById(getChat(chatKey).backend ?? DEFAULT_AGENT) ?? claudeBackend;
}

// The rest fan out across every backend, because the callers are about the pod
// rather than about one agent: a shutdown owes a word to every chat with a run
// up (restart.ts), and a chat is busy if ANY backend is running for it.

export function runningChats(): string[] {
  return [...new Set(BACKENDS.flatMap((b) => b.runningChats()))];
}

export function isRunning(chatKey: string): boolean {
  return BACKENDS.some((b) => b.isRunning(chatKey));
}

export function isWaiting(chatKey: string): boolean {
  return BACKENDS.some((b) => b.isWaiting(chatKey));
}

export function hasLiveWakeup(chatKey: string): boolean {
  return BACKENDS.some((b) => b.hasLiveWakeup(chatKey));
}

export function waitingOn(chatKey: string) {
  for (const b of BACKENDS) {
    const tasks = b.waitingOn(chatKey);
    if (tasks.length) return tasks;
  }
  return [];
}

/** Stop whatever is up for this chat, on whichever backend. Switching agents
 * mid-run is the case that makes this fan out rather than ask the chat state:
 * the state may already name the new backend. */
export function stop(chatKey: string): boolean {
  let stopped = false;
  for (const b of BACKENDS) if (b.stop(chatKey)) stopped = true;
  return stopped;
}
