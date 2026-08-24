import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "bun:test";
import { resumableBytes } from "../src/claude.ts";

// resumableBytes resolves transcripts under the real $HOME (that is where the
// claude CLI keeps them), so the fixture lives in a uniquely-named project dir
// there and is removed on exit.
const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "gw-rb-"));
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

test("missing transcript is 0, not an error", () => {
  expect(resumableBytes(cwd, "never-ran")).toBe(0);
});

test("uncompacted transcript counts in full", () => {
  write("plain", '{"type":"user"}\n'.repeat(10));
  expect(resumableBytes(cwd, "plain")).toBe('{"type":"user"}\n'.length * 10);
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
  expect(resumableBytes(cwd, "compacted")).toBe(fromMarker);
});
