import fs from "node:fs";
import path from "node:path";
import { config } from "./config.ts";

export interface ChatState {
  sessionId?: string;
  cwd: string;
  auto: boolean;
  updatedAt: string;
}

type StateFile = Record<string, ChatState>;

const statePath = path.join(config.stateDir, "state.json");
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
