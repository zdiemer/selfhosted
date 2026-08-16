import { afterAll, beforeAll, expect, test } from "bun:test";
import net from "node:net";
import {
  answerPending,
  hasPending,
  plainText,
  selectOptions,
  startApprovalServer,
  stopApprovalServer,
  type Verdict,
} from "../src/approvals.ts";
import { approvalSocketPath } from "../src/config.ts";
import { handleReaction } from "../src/router.ts";
import { getChat, updateChat } from "../src/state.ts";
import { registerTransport } from "../src/transport.ts";

// Prompts the gateway would have sent to the phone.
const sent: { chatKey: string; text: string }[] = [];
const last = () => sent[sent.length - 1]?.text ?? "";

// Prompts go out as real messages here, because answering one by reaction
// depends on knowing which message the prompt WAS.
let msgId = 0;
registerTransport("rx", {
  chunkLimit: 4000,
  async send() {
    return ++msgId;
  },
  refId: (ref) => String(ref),
});

beforeAll(async () => {
  await startApprovalServer((chatKey, text) => {
    sent.push({ chatKey, text });
    return chatKey.startsWith("rx:")
      ? Promise.resolve(++msgId as unknown as number)
      : undefined;
  });
});

afterAll(() => {
  stopApprovalServer(); // the listening socket would keep bun alive otherwise
});

/** Stand in for approve-mcp: dial the socket and wait for the verdict. */
function ask(chatKey: string, toolName: string, input: unknown): Promise<Verdict> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(approvalSocketPath);
    let buf = "";
    sock.on("connect", () =>
      sock.write(JSON.stringify({ chatKey, toolName, input }) + "\n"),
    );
    sock.on("data", (d) => {
      buf += d.toString();
      const nl = buf.indexOf("\n");
      if (nl >= 0) {
        sock.end();
        resolve(JSON.parse(buf.slice(0, nl)));
      }
    });
    sock.on("error", reject);
  });
}

/** Wait for the prompt that `ask` triggers — the socket round trip is async. */
async function nextPrompt(): Promise<string> {
  const before = sent.length;
  for (let i = 0; i < 200 && sent.length === before; i++)
    await new Promise((r) => setTimeout(r, 5));
  return last();
}

const QUESTION = {
  questions: [
    {
      question: "Which database?",
      header: "Storage",
      multiSelect: false,
      options: [
        { label: "Postgres", description: "Relational, already deployed" },
        { label: "SQLite", description: "One file, no server" },
      ],
    },
  ],
};

test("a question is rendered as numbered options, not raw json", async () => {
  const chat = "signal:+15551110001";
  const verdict = ask(chat, "AskUserQuestion", QUESTION);
  const text = await nextPrompt();

  expect(text).toContain("Storage: Which database?");
  expect(text).toContain("1. Postgres — Relational, already deployed");
  expect(text).toContain("2. SQLite");
  expect(text).toContain("type your own answer");
  expect(text).not.toContain("allow all");

  expect(answerPending(chat, "2")).toBe(true);
  expect(await verdict).toEqual({
    behavior: "allow",
    updatedInput: { ...QUESTION, answers: { "Which database?": "SQLite" } },
  });
});

test("free text answers a question as the user's own words", async () => {
  const chat = "signal:+15551110002";
  const verdict = ask(chat, "AskUserQuestion", QUESTION);
  await nextPrompt();

  expect(answerPending(chat, "neither, use the k8s one")).toBe(true);
  const v = (await verdict) as { behavior: string; updatedInput: any };
  expect(v.updatedInput.answers).toEqual({
    "Which database?": "neither, use the k8s one",
  });
});

test("multiple questions are asked one at a time", async () => {
  const chat = "signal:+15551110003";
  const input = {
    questions: [
      { question: "Which db?", options: [{ label: "PG" }, { label: "SQLite" }] },
      {
        question: "Which features?",
        multiSelect: true,
        options: [{ label: "auth" }, { label: "search" }, { label: "sync" }],
      },
    ],
  };
  const verdict = ask(chat, "AskUserQuestion", input);

  expect(await nextPrompt()).toContain("(1/2)");
  expect(answerPending(chat, "1")).toBe(true);
  expect(hasPending(chat)).toBe(true); // still open for question 2
  expect(await nextPrompt()).toContain("Which features?");

  expect(answerPending(chat, "1,3")).toBe(true);
  const v = (await verdict) as { updatedInput: any };
  expect(v.updatedInput.answers).toEqual({
    "Which db?": "PG",
    "Which features?": "auth, sync",
  });
});

test("a plan prompt shows the plan and approving leaves plan mode", async () => {
  const chat = "signal:+15551110004";
  updateChat(chat, { plan: true });
  const plan = "# Title\n\n**Step one**\n\n- do the thing\n- then `verify`";
  const verdict = ask(chat, "ExitPlanMode", { plan });
  const text = await nextPrompt();

  expect(text).toContain("📋 Plan:");
  expect(text).toContain("Step one");
  expect(text).toContain("• do the thing");
  expect(text).not.toContain("**");
  expect(text).toContain("Reply 1 approve");

  expect(answerPending(chat, "1")).toBe(true);
  expect(await verdict).toEqual({ behavior: "allow", updatedInput: { plan } });
  // Otherwise the next message walks straight back into planning.
  expect(getChat(chat).plan).toBe(false);
});

test("feedback on a plan is denied with the note attached", async () => {
  const chat = "signal:+15551110005";
  updateChat(chat, { plan: true });
  const verdict = ask(chat, "ExitPlanMode", { plan: "do it" });
  await nextPrompt();

  expect(answerPending(chat, "skip the helm part")).toBe(true);
  const v = (await verdict) as { behavior: string; message: string };
  expect(v.behavior).toBe("deny");
  expect(v.message).toContain("skip the helm part");
  expect(getChat(chat).plan).toBe(true); // still planning
});

test("a plan with no text points at the plan file instead of going blank", async () => {
  const chat = "signal:+15551110006";
  const verdict = ask(chat, "ExitPlanMode", {});
  expect(await nextPrompt()).toContain("plan file");
  answerPending(chat, "1");
  await verdict;
});

test("ordinary tools keep the 1/2/3 grammar and ignore chatter", async () => {
  const chat = "signal:+15551110007";
  const verdict = ask(chat, "Write", { file_path: "/tmp/x" });
  const text = await nextPrompt();

  expect(text).toContain("Claude wants: Write(/tmp/x)");
  expect(text).toContain("3 allow all");
  // Not an answer — the router forwards it to claude as a new message.
  expect(answerPending(chat, "sure go ahead")).toBe(false);
  expect(answerPending(chat, "1")).toBe(true);
  expect(await verdict).toEqual({
    behavior: "allow",
    updatedInput: { file_path: "/tmp/x" },
  });
});

test("auto mode allows tools unprompted but still asks a question", async () => {
  const chat = "signal:+15551110009";
  updateChat(chat, { auto: true, autoUntil: undefined, plan: false });

  // The whole point of routing auto through the relay: no prompt for a tool…
  const before = sent.length;
  expect(await ask(chat, "Write", { file_path: "/tmp/y" })).toEqual({
    behavior: "allow",
    updatedInput: { file_path: "/tmp/y" },
  });
  expect(sent.length).toBe(before);

  // …and a question still reaches the phone, with the answers field filled in.
  // Bypassed, it would come back allowed-but-unanswered and vanish.
  const verdict = ask(chat, "AskUserQuestion", QUESTION);
  expect(await nextPrompt()).toContain("Which database?");
  expect(answerPending(chat, "1")).toBe(true);
  const v = (await verdict) as { updatedInput: any };
  expect(v.updatedInput.answers).toEqual({ "Which database?": "Postgres" });

  updateChat(chat, { auto: false });
});

test("an expired auto grant prompts again", async () => {
  const chat = "signal:+15551110010";
  updateChat(chat, { auto: true, autoUntil: Date.now() - 1000 });
  const verdict = ask(chat, "Write", { file_path: "/tmp/z" });
  expect(await nextPrompt()).toContain("Claude wants: Write(/tmp/z)");
  answerPending(chat, "2");
  await verdict;
  updateChat(chat, { auto: false, autoUntil: undefined });
});

test("answering a question or a plan is acknowledged", async () => {
  const chat = "signal:+15551110011";
  const q = ask(chat, "AskUserQuestion", QUESTION);
  await nextPrompt();
  answerPending(chat, "2");
  await q;
  // Minutes can pass before claude says anything else; the reply must not look
  // like it went nowhere.
  expect(last()).toBe("✓ answered: SQLite");

  updateChat(chat, { plan: true });
  const p = ask(chat, "ExitPlanMode", { plan: "do it" });
  await nextPrompt();
  answerPending(chat, "drop the helm part");
  await p;
  expect(last()).toContain("still planning");
  expect(last()).toContain("drop the helm part");
  updateChat(chat, { plan: false });
});

test("a malformed question falls back to the plain prompt", async () => {
  const chat = "signal:+15551110008";
  const verdict = ask(chat, "AskUserQuestion", { questions: "nope" });
  expect(await nextPrompt()).toContain("Claude wants: AskUserQuestion");
  expect(answerPending(chat, "1")).toBe(true);
  await verdict;
});

test("groups are still refused outright", async () => {
  const v = (await ask("signal:g:abc", "AskUserQuestion", QUESTION)) as {
    behavior: string;
  };
  expect(v.behavior).toBe("deny");
});

test("selectOptions clamps a single-select reply to one label", () => {
  const q = { question: "q", options: [{ label: "a" }, { label: "b" }] };
  expect(selectOptions("2", q)).toBe("b");
  expect(selectOptions("1,2", q)).toBe("a");
  expect(selectOptions("9", q)).toBe("9"); // out of range = their own words
});

test("plainText strips what the surface cannot render", () => {
  expect(plainText("## H\n\n**bold** and `code`\n- item")).toBe(
    "H\n\nbold and code\n• item",
  );
});

// ---------------------------------------------------------------------------
// Answering by reaction
// ---------------------------------------------------------------------------

const OWNER = { owner: true };

test("a 👍 on the prompt allows it", async () => {
  const verdict = ask("rx:1", "Bash", { command: "ls" });
  await nextPrompt();
  // The prompt is the most recent message this transport sent.
  handleReaction("rx:1", String(msgId), "👍", OWNER);
  expect((await verdict).behavior).toBe("allow");
});

test("a 👎 on the prompt denies it", async () => {
  const verdict = ask("rx:2", "Bash", { command: "rm -rf /" });
  await nextPrompt();
  handleReaction("rx:2", String(msgId), "👎", OWNER);
  expect((await verdict).behavior).toBe("deny");
});

test("a reaction on some other message does not answer the prompt", async () => {
  const verdict = ask("rx:3", "Bash", { command: "ls" });
  await nextPrompt();
  const promptId = String(msgId);

  // 👍 on an older message — must not approve whatever happens to be pending.
  handleReaction("rx:3", String(Number(promptId) - 1), "👍", OWNER);
  // A non-owner reacting on the right message — same credential as a ! command.
  handleReaction("rx:3", promptId, "👍", { owner: false });
  // An emoji that means nothing here.
  handleReaction("rx:3", promptId, "😂", OWNER);

  let settled = false;
  void verdict.then(() => (settled = true));
  await new Promise((r) => setTimeout(r, 50));
  expect(settled).toBe(false);

  // The real one still works afterwards.
  handleReaction("rx:3", promptId, "👍", OWNER);
  expect((await verdict).behavior).toBe("allow");
});

test("typing the digit still works", async () => {
  const verdict = ask("rx:4", "Bash", { command: "ls" });
  await nextPrompt();
  expect(answerPending("rx:4", "2")).toBe(true);
  expect((await verdict).behavior).toBe("deny");
});
