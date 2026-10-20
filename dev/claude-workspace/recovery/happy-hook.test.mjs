import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { install } from "./happy-hook.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "happy-hook-test-"));
  process.env.HAPPY_WORKSPACE_RECOVERY_DIR = root;
  process.env.WORKSPACE_CONTAINER = "term";
  const messages = [];
  const events = [];
  const session = {
    workspaceRecoveryReady: true,
    sessionId: crypto.randomUUID(), getMetadata: () => ({ path: root, flavor: "codex", codexThreadId: "thread" }),
    keepAlive() {}, sendSessionProtocolMessage() {}, sendSessionEvent(event) { events.push(event); },
  };
  return { root, messages, events, session, callback: async (message) => { messages.push(message); } };
}
test("resume prompt enters the normal callback once and writes an acknowledgment", async () => {
  const f = fixture();
  const token = crypto.randomUUID();
  process.env.HAPPY_WORKSPACE_RESUME_TOKEN = token;
  process.env.HAPPY_WORKSPACE_RESUME_PROMPT = "Resume from verified progress";
  process.env.HAPPY_WORKSPACE_SETTINGS = JSON.stringify({ permissionMode: "auto" });
  await install(f.session, f.callback);
  await install(f.session, f.callback);
  assert.equal(f.messages.length, 1);
  assert.equal(f.messages[0].meta.permissionMode, "auto");
  assert.equal(f.events.length, 1);
  assert.ok(fs.existsSync(path.join(f.root, "receipts", token + ".json")));
  assert.equal(process.env.HAPPY_WORKSPACE_RESUME_TOKEN, undefined);
  f.session.keepAlive(true, "remote");
  f.session.pendingMessageCallback({ role: "user", content: { type: "text", text: "next" }, meta: { model: "chosen" } });
  const record = JSON.parse(fs.readFileSync(path.join(f.root, "live", fs.readdirSync(path.join(f.root, "live"))[0])));
  assert.equal(record.busy, true);
  assert.equal(record.settings.model, "chosen");
  assert.equal(record.container, "term");
});
test("idle restoration acknowledges reconnect without sending a new prompt", async () => {
  const f = fixture();
  const token = crypto.randomUUID();
  process.env.HAPPY_WORKSPACE_RESUME_TOKEN = token;
  process.env.HAPPY_WORKSPACE_RESUME_PROMPT = "";
  await install(f.session, f.callback);
  assert.equal(f.messages.length, 0);
  assert.ok(fs.existsSync(path.join(f.root, "receipts", token + ".json")));
});
test("Codex restoration waits for its own successful thread resume before acknowledging", async () => {
  const f = fixture();
  f.session.workspaceRecoveryReady = false;
  const token = crypto.randomUUID();
  process.env.HAPPY_WORKSPACE_RESUME_TOKEN = token;
  process.env.HAPPY_WORKSPACE_RESUME_PROMPT = "continue";
  const installed = install(f.session, f.callback);
  const receipt = path.join(f.root, "receipts", token + ".json");
  assert.equal(fs.existsSync(receipt), false);
  f.session.keepAlive(false, "remote");
  const live = path.join(f.root, "live", fs.readdirSync(path.join(f.root, "live"))[0]);
  assert.equal(JSON.parse(fs.readFileSync(live)).busy, true);
  process.emit("workspace-recovery-ready", "another-session");
  await Promise.resolve();
  assert.equal(f.messages.length, 0);
  assert.equal(fs.existsSync(receipt), false);
  process.emit("workspace-recovery-ready", f.session.sessionId);
  await installed;
  assert.equal(f.messages.length, 1);
  assert.equal(fs.existsSync(receipt), true);
});
test("an existing receipt does not inject the same recovery prompt twice", async () => {
  const f = fixture();
  const token = crypto.randomUUID();
  fs.mkdirSync(path.join(f.root, "receipts"));
  fs.writeFileSync(path.join(f.root, "receipts", token + ".json"), "{}");
  process.env.HAPPY_WORKSPACE_RESUME_TOKEN = token;
  process.env.HAPPY_WORKSPACE_RESUME_PROMPT = "already handled";
  await install(f.session, f.callback);
  assert.equal(f.messages.length, 0);
});
