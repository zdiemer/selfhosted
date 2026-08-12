import { spawn } from "node:child_process";
import { config } from "./config.ts";

// `!bash <cmd>` — a raw shell in the workspace, no model in the loop. The
// low-bandwidth surface's escape hatch: asking claude to run `kubectl get pods`
// costs a whole run and a permission round-trip, and on a plane the difference
// between one message and four matters.
//
// This is owner-only and 1:1-only (see router). It is not a widening of the
// trust boundary: the same chat can already say `!auto on` and have claude run
// anything unprompted. What it removes is the model, the latency, and the cost.

export interface BashResult {
  text: string;
  code: number | null;
  timedOut: boolean;
}

const running = new Map<string, ReturnType<typeof spawn>>();

export function isBashRunning(chatKey: string): boolean {
  return running.has(chatKey);
}

export function stopBash(chatKey: string): boolean {
  const child = running.get(chatKey);
  if (!child) return false;
  child.kill("SIGTERM");
  return true;
}

/** Runs in the chat's own cwd, so `!cwd <repo>` scopes `!bash` too. */
export function runBash(
  chatKey: string,
  command: string,
  cwd: string,
): Promise<BashResult> {
  return new Promise<BashResult>((resolve) => {
    // `bash -lc` so the login profile is sourced — PATH picks up claude, helm,
    // kubectl and the tailscale wrapper exactly as it does in /term.
    const child = spawn("bash", ["-lc", command], {
      cwd,
      env: { ...process.env, HOME: config.home },
      stdio: ["ignore", "pipe", "pipe"],
    });
    running.set(chatKey, child);

    // stdout and stderr are interleaved into one stream: a phone reads one
    // message, and "which stream was this on" is rarely the question.
    let out = "";
    let dropped = 0;
    const append = (d: unknown) => {
      const s = String(d);
      const room = config.bash.maxOutputChars - out.length;
      if (room <= 0) dropped += s.length;
      else if (s.length > room) {
        out += s.slice(0, room);
        dropped += s.length - room;
      } else out += s;
    };
    child.stdout!.on("data", append);
    child.stderr!.on("data", append);

    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      // A command ignoring SIGTERM would otherwise hold the chat's one bash
      // slot forever.
      setTimeout(() => child.kill("SIGKILL"), 5000);
    }, config.bash.timeoutMs);

    child.on("error", (err) => {
      clearTimeout(timer);
      running.delete(chatKey);
      resolve({ text: `failed to spawn bash: ${err}`, code: null, timedOut });
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      running.delete(chatKey);
      const parts = [out.trimEnd() || "(no output)"];
      if (dropped) parts.push(`…truncated (${dropped} more chars)`);
      if (timedOut)
        parts.push(`⏱ killed after ${config.bash.timeoutMs / 1000}s`);
      resolve({ text: parts.join("\n"), code, timedOut });
    });
  });
}
