import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "bun:test";
import { resumableSize } from "../src/claude.ts";

// resumableSize resolves transcripts under the real $HOME (that is where the
// claude CLI keeps them), so the fixture lives in a uniquely-named project dir
// there and is removed on exit.
const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "gw-rs-"));
const projectDir = path.join(
  os.homedir(),
  ".claude/projects",
  cwd.replace(/[/.]/g, "-"),
);
fs.mkdirSync(projectDir, { recursive: true });
afterAll(() => {
  fs.rmSync(projectDir, { recursive: true, force: true });
  fs.rmSync(cwd, { recursive: true, force: true });
});

function write(id: string, text: string): void {
  fs.writeFileSync(path.join(projectDir, `${id}.jsonl`), text);
}

/** An assistant entry as the CLI writes it, trimmed to what this reads. */
function assistant(
  usage: Record<string, number>,
  extra: Record<string, unknown> = {},
): string {
  return `${JSON.stringify({ type: "assistant", ...extra, message: { role: "assistant", usage } })}\n`;
}

test("missing transcript is zeroes, not an error", () => {
  expect(resumableSize(cwd, "never-ran")).toEqual({ bytes: 0, tokens: 0 });
});

test("uncompacted transcript counts bytes in full", () => {
  write("plain", '{"type":"user"}\n'.repeat(10));
  expect(resumableSize(cwd, "plain").bytes).toBe(
    '{"type":"user"}\n'.length * 10,
  );
});

test("only the tail after the last compact boundary counts", () => {
  // A resume rebuilds from the newest boundary; everything before it is
  // history the summary already covers.
  const head = '{"type":"user","old":true}\n'.repeat(50);
  const tail = '{"type":"system","subtype":"compact_boundary"}\n{"type":"user"}\n';
  write("compacted", head + tail);
  // Measured from the boundary marker itself — byte-exact framing of the
  // boundary line doesn't matter at MB-scale thresholds, growing head must.
  const fromMarker = tail.length - tail.indexOf('"compact_boundary"');
  expect(resumableSize(cwd, "compacted").bytes).toBe(fromMarker);
});

test("context tokens are the newest entry's whole billed prompt", () => {
  write(
    "tokens",
    assistant({ input_tokens: 10, cache_read_input_tokens: 20 }) +
      assistant({
        input_tokens: 7,
        cache_creation_input_tokens: 1000,
        cache_read_input_tokens: 150_000,
        output_tokens: 400,
      }),
  );
  // Newest wins, cache counts, output does not — it is billed as input on the
  // next turn, which is the turn being measured.
  expect(resumableSize(cwd, "tokens").tokens).toBe(151_007);
});

test("a subagent's context is not the thread's", () => {
  write(
    "sidechain",
    assistant({ input_tokens: 120_000 }) +
      assistant({ input_tokens: 900 }, { isSidechain: true }),
  );
  expect(resumableSize(cwd, "sidechain").tokens).toBe(120_000);
});

test("a transcript that states no usage reads as 0 tokens", () => {
  // Pre-first-reply, or a backend mid-write. Healthy, not suspicious.
  write("nousage", '{"type":"user"}\n'.repeat(3));
  expect(resumableSize(cwd, "nousage").tokens).toBe(0);
});

test("compaction is visible in tokens without a boundary scan", () => {
  // The post-compaction turn is billed for the summary, not the history, so
  // the token figure drops even though the file only grew.
  const history = assistant({ input_tokens: 500_000 });
  write("shrunk", history + assistant({ input_tokens: 30_000 }));
  const size = resumableSize(cwd, "shrunk");
  expect(size.tokens).toBe(30_000);
  expect(size.bytes).toBe(history.length + assistant({ input_tokens: 30_000 }).length);
});
