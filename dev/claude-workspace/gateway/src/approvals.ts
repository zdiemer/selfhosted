import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { isGroupChat } from "./chat.ts";
import { approvalSocketPath, config } from "./config.ts";

// The approve-mcp stdio server (grandchild of this process, via claude) dials
// this unix socket with {chatKey, toolName, input} and blocks until the human
// answers over chat (or the timeout denies). One pending approval per chat is
// enough: claude runs are serialized per chat, and claude itself awaits the
// permission tool before continuing.

export interface PendingApproval {
  chatKey: string;
  toolName: string;
  input: unknown;
  resolve: (verdict: Verdict) => void;
}

export type Verdict =
  | { behavior: "allow"; updatedInput: unknown }
  | { behavior: "deny"; message: string };

const pending = new Map<string, PendingApproval>();

// Tool names the user answered "3 = allow all like this" for, per session-ish
// lifetime (in-memory; resets on pod restart, which is the safe direction).
const autoApproved = new Map<string, Set<string>>();

export type ApprovalPrompt = (chatKey: string, text: string) => void;

export function hasPending(chatKey: string): boolean {
  return pending.has(chatKey);
}

/** Route a "1"/"2"/"3" chat reply to the waiting approval. Returns false if
 * the reply wasn't an answer to anything. */
export function answerPending(chatKey: string, reply: string): boolean {
  const p = pending.get(chatKey);
  if (!p) return false;
  const answer = reply.trim();
  if (!["1", "2", "3"].includes(answer)) return false;
  pending.delete(chatKey);
  if (answer === "2") {
    p.resolve({ behavior: "deny", message: "denied by user over chat" });
  } else {
    if (answer === "3") {
      let set = autoApproved.get(chatKey);
      if (!set) autoApproved.set(chatKey, (set = new Set()));
      set.add(p.toolName);
    }
    p.resolve({ behavior: "allow", updatedInput: p.input });
  }
  return true;
}

function describeTool(toolName: string, input: unknown): string {
  const i = input as Record<string, unknown> | null;
  if (toolName === "Bash" && i?.command) return `Bash(${i.command})`;
  if (i?.file_path) return `${toolName}(${i.file_path})`;
  const json = JSON.stringify(i ?? {});
  return `${toolName}(${json.length > 200 ? json.slice(0, 200) + "…" : json})`;
}

export function startApprovalServer(sendPrompt: ApprovalPrompt): void {
  fs.mkdirSync(path.dirname(approvalSocketPath), {
    recursive: true,
    mode: 0o700,
  });
  fs.rmSync(approvalSocketPath, { force: true });

  const server = net.createServer((sock) => {
    let buf = "";
    sock.on("data", (d) => {
      buf += d.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      let req: { chatKey: string; toolName: string; input: unknown };
      try {
        req = JSON.parse(buf.slice(0, nl));
      } catch {
        sock.end();
        return;
      }
      handleRequest(req, sock, sendPrompt);
    });
    sock.on("error", () => {});
  });
  server.listen(approvalSocketPath);
}

function handleRequest(
  req: { chatKey: string; toolName: string; input: unknown },
  sock: net.Socket,
  sendPrompt: ApprovalPrompt,
): void {
  const finish = (verdict: Verdict) => {
    sock.write(JSON.stringify(verdict) + "\n");
    sock.end();
  };

  // Groups are never asked. Anything outside groups.allowedTools is refused
  // here rather than relayed: the room would see the prompt, only one member
  // could answer it, and a "3 = allow all" from that member would quietly widen
  // what everyone else can reach for the rest of the session.
  if (isGroupChat(req.chatKey)) {
    finish({
      behavior: "deny",
      message:
        `${req.toolName} is not available in group chats. ` +
        "Answer from the conversation, or use WebFetch/WebSearch.",
    });
    return;
  }

  if (autoApproved.get(req.chatKey)?.has(req.toolName)) {
    finish({ behavior: "allow", updatedInput: req.input });
    return;
  }

  // A second concurrent ask for the same chat shouldn't happen (runs are
  // serialized), but deny it rather than silently replacing the first.
  if (pending.has(req.chatKey)) {
    finish({ behavior: "deny", message: "another approval is already pending" });
    return;
  }

  const timer = setTimeout(() => {
    pending.delete(req.chatKey);
    finish({
      behavior: "deny",
      message: `approval timed out after ${config.approvalTimeoutMs / 60000}m; re-send your message to retry`,
    });
    sendPrompt(req.chatKey, "⏱ approval timed out — denied.");
  }, config.approvalTimeoutMs);

  pending.set(req.chatKey, {
    ...req,
    resolve: (verdict) => {
      clearTimeout(timer);
      finish(verdict);
    },
  });

  sock.on("close", () => {
    // claude died or was !stopped while waiting; clear the prompt.
    if (pending.get(req.chatKey)?.input === req.input) {
      clearTimeout(timer);
      pending.delete(req.chatKey);
    }
  });

  sendPrompt(
    req.chatKey,
    `Claude wants: ${describeTool(req.toolName, req.input)}\n` +
      `Reply 1 allow · 2 deny · 3 allow all ${req.toolName} this session`,
  );
}
