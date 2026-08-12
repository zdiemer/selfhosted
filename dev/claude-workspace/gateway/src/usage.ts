import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { config } from "./config.ts";

// `!usage` — token accounting read straight off the PVC.
//
// There is no `claude usage` subcommand and no supported API for the
// subscription's limit counters; the interactive `/usage` panel is a TUI view
// this headless surface can't reach. What IS on disk is every run's transcript
// under ~/.claude/projects/<munged-cwd>/<session>.jsonl, and each assistant
// message carries the exact `usage` block the API returned. Summing those is
// the same arithmetic ccusage does, with no network call and no extra auth.
//
// It counts THIS workspace only — tmux, Happy, bakery-less claude runs and chat
// runs all share this $HOME, so it's the whole pod, but nothing you ran on the
// laptop.

interface Bucket {
  input: number;
  output: number;
  cacheWrite: number;
  cacheRead: number;
  messages: number;
}

interface ApiUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
}

function empty(): Bucket {
  return { input: 0, output: 0, cacheWrite: 0, cacheRead: 0, messages: 0 };
}

function add(b: Bucket, u: ApiUsage): void {
  b.input += u.input_tokens ?? 0;
  b.output += u.output_tokens ?? 0;
  b.cacheWrite += u.cache_creation_input_tokens ?? 0;
  b.cacheRead += u.cache_read_input_tokens ?? 0;
  b.messages++;
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

function line(label: string, b: Bucket): string {
  return (
    `${label}: ${fmt(b.input)} in · ${fmt(b.output)} out · ` +
    `${fmt(b.cacheWrite + b.cacheRead)} cache · ${b.messages} msgs`
  );
}

function transcripts(): string[] {
  const root = path.join(config.home, ".claude/projects");
  let dirs: string[];
  try {
    dirs = fs.readdirSync(root);
  } catch {
    return [];
  }
  const files: string[] = [];
  for (const d of dirs) {
    const dir = path.join(root, d);
    try {
      for (const f of fs.readdirSync(dir)) {
        if (f.endsWith(".jsonl")) files.push(path.join(dir, f));
      }
    } catch {
      // A project dir that vanished mid-scan (or isn't one) is not an error.
    }
  }
  return files;
}

/**
 * Sum token usage over the last `days` days, plus the current 5-hour window —
 * the subscription's limits reset on a rolling 5h block, so that number is the
 * one that answers "can I keep going right now?".
 */
export async function usageReport(days: number): Promise<string> {
  const now = Date.now();
  const windowStart = now - days * 86_400_000;
  const blockStart = now - 5 * 3_600_000;
  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);

  const window = empty();
  const block = empty();
  const today = empty();
  const byModel = new Map<string, Bucket>();
  // Assistant messages can appear more than once across transcripts (a
  // --resume rewrites the tail of the conversation into the new session's
  // file), so bill each message id once.
  const seen = new Set<string>();
  let files = 0;

  for (const file of transcripts()) {
    let stat: fs.Stats;
    try {
      stat = fs.statSync(file);
    } catch {
      continue;
    }
    // Nothing in a file untouched since before the window can be inside it.
    if (stat.mtimeMs < windowStart) continue;
    files++;

    const rl = readline.createInterface({
      input: fs.createReadStream(file, { encoding: "utf8" }),
      crlfDelay: Infinity,
    });
    for await (const raw of rl) {
      if (!raw || raw.indexOf('"usage"') < 0) continue;
      let rec: {
        type?: string;
        timestamp?: string;
        message?: { id?: string; model?: string; usage?: ApiUsage };
      };
      try {
        rec = JSON.parse(raw);
      } catch {
        continue;
      }
      if (rec.type !== "assistant") continue;
      const usage = rec.message?.usage;
      if (!usage) continue;
      const id = rec.message?.id;
      if (id) {
        if (seen.has(id)) continue;
        seen.add(id);
      }
      const ts = rec.timestamp ? Date.parse(rec.timestamp) : NaN;
      if (!Number.isFinite(ts) || ts < windowStart) continue;

      add(window, usage);
      if (ts >= blockStart) add(block, usage);
      if (ts >= dayStart.getTime()) add(today, usage);
      const model = rec.message?.model ?? "unknown";
      let m = byModel.get(model);
      if (!m) byModel.set(model, (m = empty()));
      add(m, usage);
    }
  }

  if (!window.messages)
    return `no usage recorded in the last ${days}d (${files} transcripts scanned)`;

  const models = [...byModel.entries()]
    .sort((a, b) => b[1].output - a[1].output)
    .slice(0, 4)
    .map(([model, b]) => `  ${model}: ${fmt(b.input + b.cacheWrite + b.cacheRead)} in · ${fmt(b.output)} out`);

  return [
    line("last 5h", block),
    line("today", today),
    line(`last ${days}d`, window),
    `by model (${days}d):`,
    ...models,
    "counts every claude run in this workspace (chat, tmux, Happy), from ~/.claude transcripts",
  ].join("\n");
}
