import { expect, test } from "bun:test";
import { reactionAnswer } from "../src/approvals.ts";

test("thumbs and checks allow, crosses deny", () => {
  expect(reactionAnswer("👍")).toBe("1");
  expect(reactionAnswer("✅")).toBe("1");
  expect(reactionAnswer("👎")).toBe("2");
  expect(reactionAnswer("❌")).toBe("2");
  expect(reactionAnswer("💯")).toBe("3");
});

test("skin tone and variation selectors are presentation, not meaning", () => {
  // Whichever 👍 the sender's keyboard produces is the same answer.
  expect(reactionAnswer("👍🏽")).toBe("1");
  expect(reactionAnswer("👍🏿")).toBe("1");
  expect(reactionAnswer("✅️")).toBe("1");
  expect(reactionAnswer("☑️")).toBe("1");
});

test("an unrelated reaction answers nothing", () => {
  // Reacting 😂 to a prompt must not approve it.
  for (const e of ["😂", "❤️", "🎉", "", "👀", "🕒"])
    expect(reactionAnswer(e)).toBe("");
});
