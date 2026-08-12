import { expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { attachmentPreamble, extractSendMarkers } from "../src/attachments.ts";
import { config } from "../src/config.ts";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "att-"));
const real = path.join(tmp, "chart.png");
fs.writeFileSync(real, "x");

test("a marker on its own line becomes an attachment and leaves the text", () => {
  const out = extractSendMarkers(
    `Here's the chart you asked for.\n[[send:${real}]]\nLet me know.`,
  );
  expect(out.files).toEqual([real]);
  expect(out.text).toBe("Here's the chart you asked for.\nLet me know.");
  expect(out.problems).toEqual([]);
});

test("a reply that is only an attachment isn't left as blank lines", () => {
  const out = extractSendMarkers(`[[send:${real}]]`);
  expect(out.files).toEqual([real]);
  expect(out.text).toBe("");
});

test("a marker naming something undeliverable explains itself", () => {
  // Silence here would read as the model ignoring the request.
  const missing = extractSendMarkers(`[[send:${tmp}/nope.png]]`);
  expect(missing.files).toEqual([]);
  expect(missing.problems[0]).toContain("no such file");

  const relative = extractSendMarkers("[[send:chart.png]]");
  expect(relative.problems[0]).toContain("absolute path");

  const dir = extractSendMarkers(`[[send:${tmp}]]`);
  expect(dir.problems[0]).toContain("not a file");
});

test("a file over the size limit is refused, not attempted", () => {
  const big = path.join(tmp, "big.bin");
  fs.writeFileSync(big, Buffer.alloc(config.attachments.maxBytes + 1024));
  const out = extractSendMarkers(`[[send:${big}]]`);
  expect(out.files).toEqual([]);
  expect(out.problems[0]).toContain("over the");
});

test("a marker mid-sentence is left alone", () => {
  // Explaining the feature must not trigger it.
  const out = extractSendMarkers("Write [[send:/tmp/x.png]] on its own line.");
  expect(out.files).toEqual([]);
  expect(out.text).toContain("[[send:/tmp/x.png]]");
});

test("the preamble points the run at the files, or says nothing", () => {
  expect(attachmentPreamble([])).toBe("");
  const one = attachmentPreamble(["/inbox/a.png"]);
  expect(one).toContain("1 file");
  expect(one).toContain("/inbox/a.png");
  expect(attachmentPreamble(["/a.png", "/b.png"])).toContain("2 files");
});
