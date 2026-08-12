import { beforeEach, expect, test } from "bun:test";
import { replyPrefix } from "../src/chunk.ts";
import {
  DEFAULT_SESSION,
  getChat,
  listSessions,
  sessionName,
  switchSession,
  updateChat,
} from "../src/state.ts";

const CHAT = "stdin:sessions";

beforeEach(() => {
  updateChat(CHAT, {
    sessionId: undefined,
    session: undefined,
    sessions: undefined,
  });
});

test("a chat starts on the default session", () => {
  expect(sessionName(getChat(CHAT))).toBe(DEFAULT_SESSION);
});

test("switching parks the current thread and starts the new one fresh", () => {
  updateChat(CHAT, { sessionId: "main-session-id" });
  expect(switchSession(CHAT, "api").resumed).toBe(false);

  const chat = getChat(CHAT);
  expect(chat.session).toBe("api");
  expect(chat.sessionId).toBeUndefined(); // fresh thread
  expect(chat.sessions?.main).toBe("main-session-id"); // parked, not lost
});

test("switching back resumes the parked id", () => {
  updateChat(CHAT, { sessionId: "main-session-id" });
  switchSession(CHAT, "api");
  updateChat(CHAT, { sessionId: "api-session-id" }); // a run happened on "api"

  expect(switchSession(CHAT, "main").resumed).toBe(true);
  const chat = getChat(CHAT);
  expect(chat.sessionId).toBe("main-session-id");
  expect(chat.sessions?.api).toBe("api-session-id");
  // The live one is never also parked — that's the one-place-to-update rule.
  expect(chat.sessions?.main).toBeUndefined();
});

test("switching to the session you are already on is a no-op", () => {
  updateChat(CHAT, { sessionId: "same" });
  expect(switchSession(CHAT, DEFAULT_SESSION).resumed).toBe(true);
  expect(getChat(CHAT).sessionId).toBe("same");
});

test("listing puts the current thread first and marks it", () => {
  updateChat(CHAT, { sessionId: "a" });
  switchSession(CHAT, "api");
  updateChat(CHAT, { sessionId: "b" });

  const list = listSessions(getChat(CHAT));
  expect(list[0]).toEqual({ name: "api", sessionId: "b", current: true });
  expect(list.find((s) => s.name === "main")?.sessionId).toBe("a");
  // Exactly one current, and no duplicate of it among the parked ones.
  expect(list.filter((s) => s.current).length).toBe(1);
});

test("the banner names the thread only when it isn't the default", () => {
  expect(replyPrefix("/home/node/code/selfhosted", "abcdef12", "", "main")).toBe(
    "[selfhosted · abcdef]",
  );
  expect(replyPrefix("/home/node/code/selfhosted", "abcdef12", "auto", "api")).toBe(
    "[selfhosted · api/abcdef · auto]",
  );
});
