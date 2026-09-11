import { expect, test } from "bun:test";
import { Cron } from "croner";
import { parseSchedules } from "../src/config.ts";
import {
  getChat,
  resumeIdForSlot,
  sessionPatchForSlot,
  updateChat,
} from "../src/state.ts";

// ---- parseSchedules --------------------------------------------------------

test("valid schedules parse; malformed entries are dropped, not fatal", () => {
  const raw = JSON.stringify([
    { name: "trading", cron: "45 9 * * 1-5", prompt: "check the book" },
    { name: "no-prompt", cron: "0 0 * * *" }, // dropped: no prompt
    { cron: "0 0 * * *", prompt: "x" }, // dropped: no name
    { name: "trading", cron: "1 1 * * *", prompt: "dupe" }, // dropped: dup name
  ]);
  const parsed = parseSchedules(raw);
  expect(parsed.map((s) => s.name)).toEqual(["trading"]);
});

test("empty, unset, and non-JSON GW_SCHEDULES mean no schedules", () => {
  expect(parseSchedules(undefined)).toEqual([]);
  expect(parseSchedules("")).toEqual([]);
  expect(parseSchedules("not json")).toEqual([]);
  expect(parseSchedules('{"name":"obj-not-array"}')).toEqual([]);
});

// ---- cron + timezone (the math croner owes us) -----------------------------

test("a market-day cron in America/New_York fires at the right UTC instants", () => {
  const cron = new Cron("45 9 * * 1-5", { timezone: "America/New_York" });
  // From a Saturday (UTC), next firing is Monday 9:45 ET. August = EDT
  // (UTC-4), so 13:45Z.
  const fromSat = new Date("2026-08-22T12:00:00Z");
  expect(cron.nextRun(fromSat)?.toISOString()).toBe("2026-08-24T13:45:00.000Z");
  // And across the DST boundary (Nov 1 2026): EST is UTC-5, so 14:45Z.
  const fromNov = new Date("2026-11-02T00:00:00Z");
  expect(cron.nextRun(fromNov)?.toISOString()).toBe("2026-11-02T14:45:00.000Z");
  cron.stop();
});

// ---- session slot pinning --------------------------------------------------

const CHAT = "stdin:schedules";

test("a pinned run resumes its slot, not the chat's live thread", () => {
  updateChat(CHAT, {
    session: undefined, // current = main
    sessionId: "main-live-id",
    sessions: { trading: "trading-parked-id" },
  });
  const chat = getChat(CHAT);
  expect(resumeIdForSlot(chat, "trading")).toBe("trading-parked-id");
  // …and when the pinned slot IS current, the live pointer is the right id.
  expect(resumeIdForSlot(chat, "main")).toBe("main-live-id");
  // A slot that has never run starts fresh.
  expect(resumeIdForSlot(chat, "brand-new")).toBeUndefined();
});

test("a pinned run's new id lands in its slot, never the live pointer", () => {
  updateChat(CHAT, {
    session: undefined,
    sessionId: "main-live-id",
    sessions: { trading: "old-trading-id" },
  });
  const patch = sessionPatchForSlot(getChat(CHAT), "trading", "new-trading-id");
  expect(patch).toEqual({
    sessions: { trading: "new-trading-id" },
  });
  // The chat's own thread was untouched.
  expect(patch.sessionId).toBeUndefined();
});

test("when the pinned slot is the current session, the live pointer moves", () => {
  updateChat(CHAT, {
    session: "trading",
    sessionId: "trading-live-id",
    sessions: {},
  });
  const patch = sessionPatchForSlot(getChat(CHAT), "trading", "next-id");
  expect(patch).toEqual({ sessionId: "next-id" });
});
