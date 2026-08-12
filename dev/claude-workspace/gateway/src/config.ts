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

// Appended to Claude Code's own system prompt (--append-system-prompt), not a
// replacement — the default one is what teaches it the tools it has.
const DEFAULT_SYSTEM_PROMPT = `You are reachable over Signal and WhatsApp rather than a terminal. Your reply is delivered as chat messages on a phone, often on a slow or intermittent connection.

Write plain text. Markdown is not rendered here: asterisks, backticks, and pound signs arrive as literal characters, so skip bold, italics, headers, code fences, and bullet syntax. For a list, use short lines. For a command or path, write it inline.

Lead with the answer and keep it to a few sentences unless more was asked for. Long replies are split across several messages and truncated after a few, so length costs the reader more than it costs you. Skip preamble, restating the question, and sign-offs.

You are running headless. There is no interactive terminal, and the person sees only your final message, not your tool calls or reasoning.`;

const DEFAULT_GROUP_SYSTEM_PROMPT = `This is a group chat. Everyone in the room reads your replies, but only the person who tagged you is asking — answer them, and don't address the room at large. Earlier messages from other people are given to you as context.

You have no filesystem, shell, or cluster access in a group. Answer from the conversation, or look something up on the web. If something genuinely needs the workspace, say so and suggest they ask you directly.`;

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
    // Group IDs (base64, from the signal-cli log's `Group info: Id:`). In a
    // group the GROUP is the credential, not the sender — see groups below.
    allowedGroups: envList("SIGNAL_ALLOWED_GROUPS"),
  },
  whatsapp: {
    enabled: (process.env.GW_WA_ENABLED ?? "false") === "true",
    allowedSenders: envList("WA_ALLOWED_SENDERS"),
    // Group JIDs (`…@g.us`).
    allowedGroups: envList("WA_ALLOWED_GROUPS"),
  },

  defaultCwd: process.env.GW_DEFAULT_CWD ?? path.join(home, "code/selfhosted"),
  codeRoot: process.env.GW_CODE_ROOT ?? path.join(home, "code"),
  // Model and reasoning depth for every headless run; `!model` / `!effort`
  // override per chat. Medium effort is the balance point for a surface where
  // replies are read on a phone over a bad connection.
  model: process.env.GW_MODEL ?? "claude-opus-5",
  effort: process.env.GW_EFFORT ?? "medium",
  systemPrompt: process.env.GW_SYSTEM_PROMPT ?? DEFAULT_SYSTEM_PROMPT,

  groups: {
    // Off by default, and deliberately so: a group publishes claude's output to
    // everyone in the room.
    //
    // In a group the GROUP is the credential. Any member of an allowlisted
    // group can address the bot — the personal sender allowlist is not
    // consulted. That is a far smaller grant than it sounds: group runs get
    // allowedTools only (no filesystem, shell, or cluster), no auto mode, and a
    // shared rate limit. What it does mean is that everyone in the room can
    // spend the subscription, so allowlist rooms, not just people.
    //
    // `!` commands stay owner-only everywhere (see router), so a group member
    // cannot change the model, cwd, or session.
    enabled: (process.env.GW_GROUPS_ENABLED ?? "false") === "true",
    // Only answer when actually tagged. Without this the bot replies to every
    // allowlisted message in the room, which is unbearable in a busy group.
    requireMention: (process.env.GW_GROUPS_REQUIRE_MENTION ?? "true") === "true",
    // Groups never get the 1:1 tool set. No filesystem, no shell, no writes —
    // answer from the conversation, or look something up. Anything outside this
    // list is denied outright rather than prompted (approvals.ts).
    allowedTools: process.env.GW_GROUP_ALLOWED_TOOLS ?? "WebFetch WebSearch",
    systemPrompt:
      process.env.GW_GROUP_SYSTEM_PROMPT ?? DEFAULT_GROUP_SYSTEM_PROMPT,
    // A mention means a real @-mention or a reply to the bot — structured
    // address, nothing else. Matching the bot's name in the message text was
    // tried and removed: "claude" comes up in ordinary group conversation, and
    // the bot answering that is the exact failure requireMention prevents.
    // Lines of ambient group chatter kept as context for the next mention.
    contextLines: envInt("GW_GROUP_CONTEXT_LINES", 50),
    // Runs per group per window. This is a personal subscription; a group is
    // the only surface where someone else can spend it.
    rateLimit: envInt("GW_GROUP_RATE_LIMIT", 20),
    rateWindowMs: envInt("GW_GROUP_RATE_WINDOW_MINUTES", 60) * 60_000,
  },
  bash: {
    // `!bash <cmd>` — a raw shell, no model in the loop. On by default: this is
    // owner-only in a 1:1, and the same chat can already `!auto on` and have
    // claude run anything unprompted. Off (`messaging.bash.enabled: false`)
    // makes the model the only path to the shell.
    enabled: (process.env.GW_BASH_ENABLED ?? "true") === "true",
    timeoutMs: envInt("GW_BASH_TIMEOUT_SECONDS", 120) * 1000,
    // Bounded before chunking, so a runaway `cat` can't grow the pod's heap.
    maxOutputChars: envInt("GW_BASH_MAX_OUTPUT_CHARS", 8000),
  },
  // Days of transcript history `!usage` sums when called with no argument.
  usageDays: envInt("GW_USAGE_DAYS", 7),
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
