import os from "node:os";
import path from "node:path";

function envList(name: string): string[] {
  return (process.env[name] ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function envInt(name: string, fallback: number): number {
  const n = Number(process.env[name]);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

const home = process.env.HOME ?? os.homedir();

export const config = {
  home,
  // Persistent (PVC) state: session map + Baileys auth keys.
  stateDir:
    process.env.GW_STATE_DIR ??
    path.join(home, ".config/selfhosted/messaging-gateway"),
  // Ephemeral runtime: approval socket + per-chat mcp configs. Lives on the
  // pod's shared /tmp emptyDir; nothing here needs to survive a restart.
  runtimeDir: process.env.GW_RUNTIME_DIR ?? "/tmp/messaging-gateway",
  // Where this app is installed in the image; approve-mcp.ts is spawned from
  // here by the claude child. Defaults to the source tree when run in dev.
  appDir: process.env.GW_APP_DIR ?? path.resolve(import.meta.dirname, ".."),

  signal: {
    enabled: (process.env.GW_SIGNAL_ENABLED ?? "true") === "true",
    number: process.env.SIGNAL_NUMBER ?? "",
    socket: process.env.GW_SIGNAL_SOCKET ?? "/tmp/signal-gw/signal-cli.sock",
    allowedSenders: envList("SIGNAL_ALLOWED_SENDERS"),
  },
  whatsapp: {
    enabled: (process.env.GW_WA_ENABLED ?? "false") === "true",
    allowedSenders: envList("WA_ALLOWED_SENDERS"),
  },

  defaultCwd: process.env.GW_DEFAULT_CWD ?? path.join(home, "code/selfhosted"),
  codeRoot: process.env.GW_CODE_ROOT ?? path.join(home, "code"),
  approvalTimeoutMs: envInt("GW_APPROVAL_TIMEOUT_SECONDS", 300) * 1000,
  claudeTimeoutMs: envInt("GW_CLAUDE_TIMEOUT_SECONDS", 1800) * 1000,
  // Read-only tools that never prompt; everything else goes through the
  // approval relay. Space-separated, claude --allowedTools syntax.
  allowedTools:
    process.env.GW_ALLOWED_TOOLS ??
    "Read Glob Grep LS WebFetch WebSearch TodoWrite " +
      "Bash(git status:*) Bash(git log:*) Bash(git diff:*) Bash(ls:*) Bash(rg:*)",
  maxConcurrentClaude: envInt("GW_MAX_CONCURRENT", 2),
  queueDepth: envInt("GW_QUEUE_DEPTH", 5),
};

export const approvalSocketPath = path.join(config.runtimeDir, "approve.sock");
