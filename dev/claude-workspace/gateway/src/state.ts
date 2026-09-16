import fs from "node:fs";
import path from "node:path";
import { config } from "./config.ts";

/**
 * One agent's threads in a chat. Each backend keeps its own: switching to codex
 * and back must land in the claude conversation you left, not a fresh one, and
 * the ids are not interchangeable anyway.
 *
 * claude's thread is the TOP LEVEL of ChatState rather than an entry in
 * `backends` — see threadOf. That is not tidiness, it is the migration: every
 * state.json written before this existed has claude's ids at the top level, and
 * leaving them there means there is nothing to migrate and no version to check.
 */
export interface AgentThread {
  sessionId?: string;
  session?: string;
  sessions?: Record<string, string>;
  model?: string;
}

export interface ChatState {
  /** Which agent this chat is pointed at (!agent). Unset means claude, which is
   * what every chat was before there was a choice. */
  backend?: string;
  /** Threads for every agent EXCEPT claude, whose thread is the top level. */
  backends?: Record<string, AgentThread>;
  sessionId?: string;
  cwd: string;
  auto: boolean;
  /** `!plan on` — runs go out with `--permission-mode plan`, so claude
   * researches and proposes instead of editing. Mutually exclusive with auto:
   * one says "don't touch anything", the other "don't ask". */
  plan?: boolean;
  /** Name of the session `sessionId` currently belongs to. Unset means the
   * default one, which is what every chat had before named sessions existed. */
  session?: string;
  /** Parked sessions by name, so one chat can hold several threads at once.
   * The CURRENT one lives in `sessionId`; this is everything else. */
  sessions?: Record<string, string>;
  /** Epoch ms at which `auto` lapses back to prompting. Unset means auto is
   * open-ended, which is what `!auto on` still gives you. */
  autoUntil?: number;
  /** Per-chat overrides for config.model / config.effort, set with !model and
   * !effort. Unset means "follow the chart default". */
  model?: string;
  effort?: string;
  /** How closely this chat's live status message follows a run — a
   * config.VERBOSITY_MS level name, set with !verbose. Unset means the chart
   * default. Per chat rather than per run because it is a property of how you
   * read this thread, not of the question you just asked. */
  verbosity?: string;
  /** A run was started and has not finished. Survives on the PVC precisely so
   * the *next* process can see it: a true here after a boot means the pod went
   * down mid-run, which is what the restart notice reports. */
  inFlight?: boolean;
  /** A ScheduleWakeup the model armed and the gateway now owes it (wakeup.ts,
   * where this shape is PendingWakeup — duplicated here because importing it
   * would be a cycle). On the PVC for the same reason the session id is: a
   * redeploy must postpone a wake-up, not erase it. */
  wakeup?: {
    at: number;
    prompt: string;
    run?: {
      cwd?: string;
      session?: string;
      fresh?: boolean;
      model?: string;
      effort?: string;
    };
    schedName?: string;
  };
  updatedAt: string;
}

type StateFile = Record<string, ChatState>;

const statePath = path.join(config.stateDir, "state.json");
const bootPath = path.join(config.stateDir, "boot.json");
let cache: StateFile | null = null;

function load(): StateFile {
  if (cache) return cache;
  try {
    cache = JSON.parse(fs.readFileSync(statePath, "utf8")) as StateFile;
  } catch {
    cache = {};
  }
  return cache;
}

function save(): void {
  fs.mkdirSync(config.stateDir, { recursive: true, mode: 0o700 });
  // Atomic replace so a crash mid-write can't truncate the session map.
  const tmp = statePath + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(cache, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, statePath);
}

export function getChat(chatKey: string): ChatState {
  const all = load();
  return (
    all[chatKey] ?? {
      cwd: config.defaultCwd,
      auto: false,
      updatedAt: new Date().toISOString(),
    }
  );
}

export function updateChat(
  chatKey: string,
  patch: Partial<ChatState>,
): ChatState {
  const all = load();
  const next: ChatState = {
    ...getChat(chatKey),
    ...patch,
    updatedAt: new Date().toISOString(),
  };
  all[chatKey] = next;
  save();
  return next;
}

export const DEFAULT_SESSION = "main";

/** claude's thread IS the chat state, so this returns the chat itself for it —
 * structurally an AgentThread, and the reason old state files need no
 * migration. Everything else lives under `backends`. */
export function threadOf(chat: ChatState, backend: string): AgentThread {
  return backend === "claude" ? chat : (chat.backends?.[backend] ?? {});
}

/** The updateChat patch that writes `patch` into `backend`'s thread. Flat for
 * claude; a merged sub-record for everything else, because updateChat is a
 * shallow spread and would otherwise drop the other agents' threads. */
export function threadPatch(
  chat: ChatState,
  backend: string,
  patch: AgentThread,
): Partial<ChatState> {
  if (backend === "claude") return patch;
  return {
    backends: {
      ...(chat.backends ?? {}),
      [backend]: { ...(chat.backends?.[backend] ?? {}), ...patch },
    },
  };
}

export function sessionName(thread: AgentThread): string {
  return thread.session ?? DEFAULT_SESSION;
}

/**
 * Park the current session under its name and make `name` current. Switching
 * to a name that has never been used starts a fresh thread rather than
 * erroring — naming it is how you create it.
 *
 * Only one id is ever "live" (`sessionId`); the map holds the parked ones. The
 * alternative, keeping every id in the map and a pointer beside it, would mean
 * two places to keep in step every time a run writes a new session id.
 */
export function switchSession(
  chatKey: string,
  backend: string,
  name: string,
): { resumed: boolean } {
  const chat = getChat(chatKey);
  const thread = threadOf(chat, backend);
  const from = sessionName(thread);
  if (from === name) return { resumed: Boolean(thread.sessionId) };

  const sessions = { ...(thread.sessions ?? {}) };
  if (thread.sessionId) sessions[from] = thread.sessionId;
  else delete sessions[from];

  const target = sessions[name];
  delete sessions[name]; // it is the live one now, not a parked one
  updateChat(
    chatKey,
    threadPatch(chat, backend, { session: name, sessionId: target, sessions }),
  );
  return { resumed: Boolean(target) };
}

/** The id a run pinned to session slot `slot` should resume: the live id if
 * that slot is the chat's current session, else the parked one. Undefined
 * means the slot has never run — start fresh. */
export function resumeIdForSlot(
  thread: AgentThread,
  slot: string,
): string | undefined {
  return sessionName(thread) === slot
    ? thread.sessionId
    : thread.sessions?.[slot];
}

/** Where a pinned run's new session id lands: the live pointer when its slot
 * is current, the parked map otherwise — never the OTHER thread's pointer,
 * which is the whole point of pinning. */
export function sessionPatchForSlot(
  chat: ChatState,
  backend: string,
  slot: string,
  id: string,
): Partial<ChatState> {
  const thread = threadOf(chat, backend);
  const patch: AgentThread =
    sessionName(thread) === slot
      ? { sessionId: id }
      : { sessions: { ...(thread.sessions ?? {}), [slot]: id } };
  return threadPatch(chat, backend, patch);
}

/** Every session this thread holds, current first. */
export function listSessions(
  thread: AgentThread,
): { name: string; sessionId?: string; current: boolean }[] {
  const current = sessionName(thread);
  return [
    { name: current, sessionId: thread.sessionId, current: true },
    ...Object.entries(thread.sessions ?? {})
      .filter(([name]) => name !== current)
      .map(([name, sessionId]) => ({ name, sessionId, current: false })),
  ];
}

/**
 * Is auto mode actually in force? `auto` alone is not the answer once a
 * deadline is attached, and this is checked at every use rather than swept by
 * a timer: a lapse that only takes effect while the pod happens to be up would
 * be worse than no deadline at all.
 */
export function autoActive(chat: ChatState): boolean {
  // The instance-level switch is checked here rather than only in the router,
  // because state.json outlives the config: a chat that ran `!auto on` before
  // the flag was turned off would otherwise keep bypassing permissions until
  // someone thought to run `!auto off`.
  if (!config.autoEnabled) return false;
  return Boolean(chat.auto) && !autoExpired(chat);
}

export function autoExpired(chat: ChatState): boolean {
  return Boolean(chat.auto && chat.autoUntil && Date.now() > chat.autoUntil);
}

/** Every chat the state file knows about, newest activity first. */
export function allChats(): { chatKey: string; chat: ChatState }[] {
  return Object.entries(load())
    .map(([chatKey, chat]) => ({ chatKey, chat }))
    .sort((a, b) => (a.chat.updatedAt < b.chat.updatedAt ? 1 : -1));
}

/**
 * Timestamp of the last restart notice, so a crashlooping pod can't message
 * every chat once a minute. Separate from state.json because it is about the
 * process, not about any chat.
 */
export function lastBootNotice(): number {
  try {
    return Number(JSON.parse(fs.readFileSync(bootPath, "utf8")).at ?? 0);
  } catch {
    return 0;
  }
}

export function recordBootNotice(at = Date.now()): void {
  fs.mkdirSync(config.stateDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(bootPath, JSON.stringify({ at }) + "\n", { mode: 0o600 });
}
