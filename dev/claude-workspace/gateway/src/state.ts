import fs from "node:fs";
import path from "node:path";
import { config } from "./config.ts";

export interface ChatState {
  sessionId?: string;
  cwd: string;
  auto: boolean;
  /** `!plan on` — runs go out with `--permission-mode plan`, so claude
   * researches and proposes instead of editing. Mutually exclusive with auto:
   * one says "don't touch anything", the other "don't ask". */
  plan?: boolean;
  /** Per-chat overrides for config.model / config.effort, set with !model and
   * !effort. Unset means "follow the chart default". */
  model?: string;
  effort?: string;
  /** A run was started and has not finished. Survives on the PVC precisely so
   * the *next* process can see it: a true here after a boot means the pod went
   * down mid-run, which is what the restart notice reports. */
  inFlight?: boolean;
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
