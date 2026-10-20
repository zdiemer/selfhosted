// Runs inside each Happy session. No encryption keys or message text are
// persisted here; Happy owns its reconnect credentials in sessions.json.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

export function identity(pid = process.pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return fs.readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() + ":" +
      stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19];
  } catch { return undefined; }
}
function atomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(data), { mode: 0o600 });
  fs.renameSync(temporary, file);
}
const installed = new WeakSet();
export async function install(session, callback) {
  if (installed.has(session)) return;
  installed.add(session);
  const root = process.env.HAPPY_WORKSPACE_RECOVERY_DIR ??
    path.join(process.env.HOME, ".happy", "workspace-recovery");
  const name = crypto.createHash("sha256").update(session.sessionId).digest("hex");
  const file = path.join(root, "live", name + ".json");
  const token = process.env.HAPPY_WORKSPACE_RESUME_TOKEN;
  const prompt = process.env.HAPPY_WORKSPACE_RESUME_PROMPT;
  // Never leak a recovery prompt/token into agents launched by this session.
  delete process.env.HAPPY_WORKSPACE_RESUME_TOKEN;
  delete process.env.HAPPY_WORKSPACE_RESUME_PROMPT;
  let settings = {};
  try { settings = JSON.parse(process.env.HAPPY_WORKSPACE_SETTINGS ?? "{}"); } catch {}
  delete process.env.HAPPY_WORKSPACE_SETTINGS;
  let thinking = false;
  let awaitingRecovery = Boolean(token && prompt);
  let recoveryCount = Number(process.env.HAPPY_WORKSPACE_RECOVERY_COUNT ?? 0);
  delete process.env.HAPPY_WORKSPACE_RECOVERY_COUNT;
  const startedAt = Date.now();
  let mode = "remote";
  const subagents = new Set();
  let lastWrite = 0;
  let stopping = false;
  let previous = {};
  try { previous = JSON.parse(fs.readFileSync(file, "utf8")); } catch {}
  function save(state = "running", force = false) {
    if (!force && Date.now() - lastWrite < 5000) return;
    lastWrite = Date.now();
    const metadata = session.getMetadata() ?? {};
    atomic(file, {
      sessionId: session.sessionId, container: process.env.WORKSPACE_CONTAINER ?? "happy-daemon",
      podUid: process.env.WORKSPACE_POD_UID, pid: process.pid, identity: identity(),
      state: ["archived", "archiveRequested"].includes(metadata.lifecycleState) ? "stopped" : state,
      busy: awaitingRecovery || thinking || subagents.size > 0, mode, settings, recoveryCount,
      updatedAt: Date.now(), metadata: {
        path: metadata.path, host: metadata.host, flavor: metadata.flavor,
        claudeSessionId: metadata.claudeSessionId ?? previous.metadata?.claudeSessionId,
        codexThreadId: metadata.codexThreadId ?? previous.metadata?.codexThreadId,
      }, resumeToken: token,
    });
  }
  // Keep the session's own behavior; observe, don't replace, its heartbeat.
  const keepAlive = session.keepAlive.bind(session);
  session.keepAlive = (busy, currentMode) => {
    thinking = busy;
    if (!awaitingRecovery && !busy && !subagents.size && Date.now() - startedAt > 60_000) recoveryCount = 0;
    mode = currentMode;
    save();
    return keepAlive(busy, currentMode);
  };
  const protocol = session.sendSessionProtocolMessage.bind(session);
  session.sendSessionProtocolMessage = (envelope) => {
    if (envelope.subagent && envelope.ev?.t === "start") subagents.add(envelope.subagent);
    if (envelope.subagent && ["stop", "turn-end"].includes(envelope.ev?.t)) subagents.delete(envelope.subagent);
    save();
    return protocol(envelope);
  };
  session.pendingMessageCallback = (message) => {
    if (message.meta?.source !== "workspace-recovery") recoveryCount = 0;
    for (const key of ["permissionMode", "model", "effort"]) {
      if (typeof message.meta?.[key] === "string") settings[key] = message.meta[key];
    }
    thinking = true;
    save("running", true);
    return callback(message);
  };
  // A clean exit is not a restart request. Container termination is captured
  // by the supervisor's shutdown marker; SIGKILL leaves the last live record.
  process.once("exit", () => { stopping = true; save("stopped", true); });
  const heartbeat = setInterval(() => { if (!stopping) save(); }, 5000);
  heartbeat.unref();
  save("running", true);
  if (token && /^[a-f0-9-]{36}$/.test(token)) {
    // onUserMessage is registered before Codex's thread/resume can fail.
    // Do not acknowledge that early callback as a successful restoration.
    if (session.getMetadata()?.flavor === "codex" && !session.workspaceRecoveryReady) {
      await new Promise(resolve => {
        const ready = sessionId => {
          if (sessionId !== session.sessionId) return;
          process.off("workspace-recovery-ready", ready);
          resolve();
        };
        process.on("workspace-recovery-ready", ready);
      });
    }
    const receipt = path.join(root, "receipts", token + ".json");
    if (!fs.existsSync(receipt)) {
      if (prompt) {
        session.sendSessionEvent({ type: "message", message: "Workspace restarted — checking progress and continuing interrupted work." });
        await session.pendingMessageCallback({
          role: "user", content: { type: "text", text: prompt },
          meta: { ...settings, source: "workspace-recovery" },
        });
      }
      atomic(receipt, { sessionId: session.sessionId, deliveredAt: Date.now() });
    }
    awaitingRecovery = false;
    save("running", true);
  }
}
