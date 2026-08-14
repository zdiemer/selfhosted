import { expect, test } from "bun:test";
import {
  canEdit,
  editMsg,
  markReady,
  onTransportReady,
  overflowSize,
  reactTo,
  registerTransport,
  sendOne,
  sendReply,
  sendTo,
  takeOverflow,
  type MsgRef,
} from "../src/transport.ts";

// A surface that records what it was asked to do. `send` hands back the index
// of the message so the tests can tell chunks apart.
const sent: string[] = [];
const reacted: { target: MsgRef; emoji: string; remove?: boolean }[] = [];
const edited: { target: MsgRef; text: string }[] = [];

registerTransport("fake", {
  chunkLimit: 20,
  async send(_c, text) {
    sent.push(text);
    return sent.length - 1;
  },
  async react(_c, target, emoji, remove) {
    reacted.push({ target, emoji, remove });
  },
  async edit(_c, target, text) {
    edited.push({ target, text });
  },
});

// A surface with neither reactions nor edits — the contract says it still works.
registerTransport("plain", {
  chunkLimit: 20,
  async send() {
    return undefined;
  },
});

// Every acknowledgement path throws here; none of it may reach the caller.
registerTransport("broken", {
  chunkLimit: 20,
  async send() {
    return 1;
  },
  async react() {
    throw new Error("reaction rejected");
  },
  async edit() {
    throw new Error("edit window closed");
  },
});

test("sendTo splits a long reply and hands back the first chunk", async () => {
  sent.length = 0;
  const ref = await sendTo("fake:1", "line one\nline two\nline three");
  expect(sent.length).toBeGreaterThan(1);
  expect(ref).toBe(0);
});

test("sendOne truncates instead of splitting — an edit target stays one message", async () => {
  sent.length = 0;
  const ref = await sendOne("fake:1", "x".repeat(50));
  expect(sent).toEqual(["x".repeat(19) + "…"]);
  expect(ref).toBe(0);
});

test("reactions and edits reach the transport", async () => {
  reacted.length = 0;
  edited.length = 0;
  await reactTo("fake:1", 7, "👀");
  await reactTo("fake:1", 7, "👀", true);
  expect((await editMsg("fake:1", 7, "updated")).ok).toBe(true);
  expect(reacted).toEqual([
    { target: 7, emoji: "👀", remove: false },
    { target: 7, emoji: "👀", remove: true },
  ]);
  expect(edited).toEqual([{ target: 7, text: "updated" }]);
});

test("a surface without reactions or edits is skipped, not an error", async () => {
  await reactTo("plain:1", 1, "👀");
  expect(canEdit("plain:1")).toBe(false);
  expect((await editMsg("plain:1", 1, "x")).ok).toBe(false);
});

test("a missing target is skipped — an undefined ref is not an edit of nothing", async () => {
  reacted.length = 0;
  await reactTo("fake:1", undefined, "👀");
  expect(reacted).toEqual([]);
  expect((await editMsg("fake:1", undefined, "x")).ok).toBe(false);
});

test("a failed acknowledgement never propagates", async () => {
  // The whole point: a reaction the network refused must not take down the
  // message handling that triggered it.
  await reactTo("broken:1", 1, "👀");
  expect((await editMsg("broken:1", 1, "x")).ok).toBe(false);
});

test("ready fires once per surface, and late subscribers still hear it", () => {
  const seen: string[] = [];
  markReady("fake");
  markReady("fake"); // a reconnect is not a restart
  onTransportReady((p) => seen.push(p));
  expect(seen).toEqual(["fake"]);
});

test("sendReply keeps what didn't fit so !more can page it", async () => {
  sent.length = 0;
  const long = Array.from({ length: 40 }, (_, i) => `line ${i} padding padding`).join("\n");
  await sendReply("fake:pager", long);
  // Capped at 4 chunks, and the last one says how to get the rest.
  expect(sent.length).toBe(4);
  expect(sent[3]).toMatch(/…\+\d+ more chars — !more$/);
  expect(overflowSize("fake:pager")).toBeGreaterThan(0);

  // Paging chains: the remainder goes back through sendReply and leaves its own.
  const rest = takeOverflow("fake:pager");
  expect(overflowSize("fake:pager")).toBe(0);
  sent.length = 0;
  await sendReply("fake:pager", rest);
  expect(sent.length).toBeGreaterThan(0);
});

test("a reply that fits clears any stale remainder", async () => {
  await sendReply("fake:pager", "x".repeat(500));
  expect(overflowSize("fake:pager")).toBeGreaterThan(0);
  await sendReply("fake:pager", "short");
  expect(overflowSize("fake:pager")).toBe(0);
});

test("plain sendTo leaves the pager alone", async () => {
  // A !status reply between the truncated answer and !more must not eat it.
  await sendReply("fake:pager", "y".repeat(500));
  const before = overflowSize("fake:pager");
  await sendTo("fake:pager", "cwd: /home/node");
  expect(overflowSize("fake:pager")).toBe(before);
});
