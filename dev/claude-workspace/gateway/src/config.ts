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

You can send files. Put [[send:/absolute/path]] alone on a line and that file is delivered as an attachment and the marker removed from your reply — use it for screenshots, generated charts, diffs too long to read as text, or any file they ask for. Files they send you arrive as local paths in the message; read them like any other file.

You are running headless. There is no interactive terminal. A status message shows the person a one-line summary of each tool call as you work, so they can see that something is happening, but your reasoning is not shown and the reply is what they will actually read — write it as though it stands alone.`;

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

  // The live status message: one message per run, edited in place as tool
  // calls land. Off makes the surface behave as it did before — the answer,
  // and nothing until the answer.
  progress: {
    enabled: (process.env.GW_PROGRESS_ENABLED ?? "true") === "true",
    // Redraw cadence, and so also the minimum gap between edits. Baileys is an
    // unofficial WhatsApp client (see the chart README on ban risk), so it
    // keeps the conservative default; Signal talks to signal-cli over a local
    // socket and sets its own, faster interval when it registers.
    editIntervalMs: envInt("GW_PROGRESS_EDIT_SECONDS", 3) * 1000,
    // Signal's cadence. The clock ticks on its own timer, so this is about how
    // often a still-running status redraws — five seconds reads as alive
    // without turning a long run into hundreds of revisions of one message.
    signalIntervalMs: envInt("GW_PROGRESS_SIGNAL_EDIT_SECONDS", 5) * 1000,
  },
  // Reactions on the sender's own message: accepted → working → done/failed.
  // This is the one feature that shows up in a group whether the room asked
  // for it or not, hence its own switch.
  reactions: {
    enabled: (process.env.GW_REACTIONS_ENABLED ?? "true") === "true",
    queued: process.env.GW_REACT_QUEUED ?? "🕒",
    working: process.env.GW_REACT_WORKING ?? "👀",
    done: process.env.GW_REACT_DONE ?? "✅",
    error: process.env.GW_REACT_ERROR ?? "❌",
  },
  restartNotice: {
    // A redeploy of this chart is a `Recreate` rollout: the pod is torn down
    // before the new one pulls, so a run in flight simply stops. Both halves of
    // that — a word on the way down, a word on the way back — are the point.
    enabled: (process.env.GW_RESTART_NOTICE_ENABLED ?? "true") === "true",
    // Only chats active this recently hear about a restart. An hour is roughly
    // "you are still holding the phone"; a day would be a stranger's ping.
    withinMs: envInt("GW_RESTART_NOTICE_MINUTES", 60) * 60_000,
    // Suppress the notice if the last one was this recent — a crashloop must
    // not turn into a message per restart.
    dedupeMs: envInt("GW_RESTART_NOTICE_DEDUPE_MINUTES", 10) * 60_000,
    // Hard cap on how long SIGTERM handling may spend on the network before
    // exiting anyway. The pod's terminationGracePeriodSeconds is the real
    // ceiling; being SIGKILLed mid-send is a normal outcome here.
    shutdownGraceMs: envInt("GW_SHUTDOWN_GRACE_SECONDS", 5) * 1000,
  },

  attachments: {
    // Photos in, files out. A phone's camera is the fastest way to show the
    // workspace an error on a screen, and a rendered chart or screenshot is
    // the one kind of answer this surface could never give.
    enabled: (process.env.GW_ATTACHMENTS_ENABLED ?? "true") === "true",
    // Where inbound media is saved for claude to Read. Under the PVC's cache
    // rather than a repo: these are conversation artefacts, not source.
    inboxDir:
      process.env.GW_ATTACHMENT_DIR ??
      path.join(home, ".cache/messaging-gateway/inbox"),
    // Both directions. Signal's own ceiling is ~100MB and WhatsApp's ~16MB for
    // media, but this is a phone on a bad connection — the constraint is the
    // link, not the protocol.
    maxBytes: envInt("GW_ATTACHMENT_MAX_MB", 8) * 1024 * 1024,
    // Where signal-cli drops what it downloads. Inbound attachments are named
    // by id there, sometimes with an extension appended.
    signalStore:
      process.env.GW_SIGNAL_ATTACHMENT_DIR ??
      path.join(home, ".local/share/signal-cli/attachments"),
  },

  defaultCwd: process.env.GW_DEFAULT_CWD ?? path.join(home, "code/selfhosted"),
  codeRoot: process.env.GW_CODE_ROOT ?? path.join(home, "code"),
  // Directories `!cwd` is allowed to land in. Empty (the default) means
  // anywhere, which is right for an instance whose PVC only ever holds one
  // person's own repos. A scoped instance — one deployed for someone who should
  // reach exactly one project — sets GW_CWD_ROOTS, and then `!cwd` refuses any
  // path outside them. Compared as resolved paths, not string prefixes, so
  // `../` cannot walk out and `/home/node/code/rachelfreeman-notes` does not
  // match a root of `/home/node/code/rachelfreeman`.
  cwdRoots: envList("GW_CWD_ROOTS").map((p) => path.resolve(p)),
  // `!auto` hands the run --permission-mode bypassPermissions: no approval
  // relay, no deny rules, nothing between the model and the pod. That is a
  // reasonable trade for your own instance and not for a delegated one, so it
  // is a switch rather than an assumption. Off means every mutation outside
  // allowedTools round-trips to the phone.
  autoEnabled: (process.env.GW_AUTO_ENABLED ?? "true") === "true",
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

// Is `target` inside one of config.cwdRoots? True for everything when no roots
// are configured — the containment is opt-in, so an unscoped instance behaves
// exactly as it did before this existed.
//
// Both sides are resolved first. A raw `startsWith` would accept
// `/home/node/code/rachelfreeman-backup` against a root of
// `/home/node/code/rachelfreeman`, and would accept any `..` the caller cared
// to write; comparing resolved paths with a trailing separator does neither.
// `roots` is a parameter with a default rather than a straight read of config
// so the rule can be tested without a module-load dance to get GW_CWD_ROOTS in
// place before config.ts is first imported.
export function isWithinCwdRoots(
  target: string,
  roots: string[] = config.cwdRoots,
): boolean {
  if (roots.length === 0) return true;
  const resolved = path.resolve(target);
  return roots
    .map((r) => path.resolve(r))
    .some((root) => resolved === root || resolved.startsWith(root + path.sep));
}
