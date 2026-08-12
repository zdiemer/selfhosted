import fs from "node:fs";
import path from "node:path";
import { config } from "./config.ts";

// Files in both directions.
//
// Inbound: whatever the phone sent is written under config.attachments.inboxDir
// and the run is told the paths, so claude can Read them like any other file.
// Outbound: claude asks for a file to be delivered by putting a marker on its
// own line, which the gateway strips out of the reply and turns into a real
// attachment.

/** Marker claude writes to attach a file. On its own line, absolute path. */
const SEND_MARKER = /^\s*\[\[send:([^\]]+)\]\]\s*$/;

export interface OutboundFiles {
  /** The reply with the markers taken out. */
  text: string;
  /** Absolute paths to deliver, in the order they appeared. */
  files: string[];
  /** Markers that named something undeliverable, already explained. */
  problems: string[];
}

/**
 * Pull `[[send:/path]]` markers out of a reply.
 *
 * Rejects here rather than at send time so the reason reaches the person: a
 * marker that silently produced nothing would read as the model ignoring them.
 */
export function extractSendMarkers(text: string): OutboundFiles {
  const files: string[] = [];
  const problems: string[] = [];
  const kept: string[] = [];

  for (const line of text.split("\n")) {
    const m = SEND_MARKER.exec(line);
    if (!m) {
      kept.push(line);
      continue;
    }
    const file = m[1].trim();
    if (!path.isAbsolute(file)) {
      problems.push(`⚠ can't send ${file} — needs an absolute path`);
      continue;
    }
    let stat: fs.Stats;
    try {
      stat = fs.statSync(file);
    } catch {
      problems.push(`⚠ can't send ${path.basename(file)} — no such file`);
      continue;
    }
    if (!stat.isFile()) {
      problems.push(`⚠ can't send ${path.basename(file)} — not a file`);
      continue;
    }
    if (stat.size > config.attachments.maxBytes) {
      problems.push(
        `⚠ can't send ${path.basename(file)} — ${Math.round(stat.size / 1024 / 1024)}MB, ` +
          `over the ${Math.round(config.attachments.maxBytes / 1024 / 1024)}MB limit`,
      );
      continue;
    }
    files.push(file);
  }

  // Collapse the hole the markers left, so a reply that was just an image
  // isn't a stack of blank lines.
  return {
    text: kept.join("\n").replace(/\n{3,}/g, "\n\n").trim(),
    files,
    problems,
  };
}

/** Per-chat inbox directory, created on demand. */
function inboxFor(chatKey: string): string {
  const dir = path.join(
    config.attachments.inboxDir,
    chatKey.replace(/[^A-Za-z0-9]+/g, "-"),
  );
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

/** A filename that can't escape the inbox or collide with the last one. */
function safeName(name: string, fallbackExt = ""): string {
  const base = path
    .basename(name || "file")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .slice(-60);
  const stamped = `${Date.now()}-${base || "file"}`;
  return path.extname(stamped) ? stamped : stamped + fallbackExt;
}

/** Save inbound bytes and return the path to hand to claude. */
export function saveInbound(
  chatKey: string,
  name: string,
  data: Buffer,
  fallbackExt = "",
): string | undefined {
  if (data.length > config.attachments.maxBytes) {
    console.warn(
      `attachment from ${chatKey} is ${data.length} bytes; over the limit, dropped`,
    );
    return undefined;
  }
  const file = path.join(inboxFor(chatKey), safeName(name, fallbackExt));
  fs.writeFileSync(file, data, { mode: 0o600 });
  return file;
}

/**
 * Copy an attachment signal-cli already downloaded into the inbox.
 *
 * It stores them by id, sometimes with an extension appended — so look for the
 * bare id first, then anything that starts with it.
 */
export function importSignalAttachment(
  chatKey: string,
  id: string,
  filename?: string,
): string | undefined {
  const store = config.attachments.signalStore;
  let src = path.join(store, id);
  if (!fs.existsSync(src)) {
    const match = fs
      .readdirSync(store)
      .find((f) => f === id || f.startsWith(`${id}.`));
    if (!match) return undefined;
    src = path.join(store, match);
  }
  try {
    const data = fs.readFileSync(src);
    return saveInbound(chatKey, filename || path.basename(src), data);
  } catch (err) {
    console.warn(`signal: could not import attachment: ${(err as Error).message}`);
    return undefined;
  }
}

/** The line prepended to a run's prompt so claude knows the files are there. */
export function attachmentPreamble(files: string[]): string {
  if (!files.length) return "";
  return (
    `The user attached ${files.length} file${files.length === 1 ? "" : "s"} ` +
    `to this message, already saved locally. Read ${files.length === 1 ? "it" : "them"} ` +
    `if the message is about ${files.length === 1 ? "it" : "them"}:\n` +
    files.map((f) => `- ${f}`).join("\n")
  );
}

/** Drop inbox files older than a week — this is a cache, not an archive. */
export function pruneInbox(maxAgeMs = 7 * 86_400_000): void {
  const root = config.attachments.inboxDir;
  if (!fs.existsSync(root)) return;
  const cutoff = Date.now() - maxAgeMs;
  for (const dir of fs.readdirSync(root)) {
    const full = path.join(root, dir);
    try {
      for (const f of fs.readdirSync(full)) {
        const file = path.join(full, f);
        if (fs.statSync(file).mtimeMs < cutoff) fs.unlinkSync(file);
      }
    } catch {
      // A racing write or a vanished directory is not worth failing startup.
    }
  }
}
