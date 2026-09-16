import { expect, test } from "bun:test";
import { deliveryKey, isGroupChat, laneKey, laneOf } from "../src/chat.ts";
import {
  overflowSize,
  registerTransport,
  sendReply,
  sendTo,
  takeOverflow,
} from "../src/transport.ts";

// Schedules run under `<chat>#<lane>` so their queue, process, wake-up and
// prompts are not the chat's — but what they say still lands in that chat.

const to: string[] = [];
registerTransport("lane", {
  chunkLimit: 10,
  async send(chatKey) {
    to.push(chatKey);
    return to.length;
  },
});

test("lane keys round-trip and never nest", () => {
  const key = laneKey("signal:abc-123", "trading");
  expect(key).toBe("signal:abc-123#trading");
  expect(deliveryKey(key)).toBe("signal:abc-123");
  expect(laneOf(key)).toBe("trading");
  expect(laneOf("signal:abc-123")).toBeUndefined();
  expect(laneKey(key, "other")).toBe("signal:abc-123#other");
});

test("a laned group or 1:1 keeps its kind", () => {
  expect(isGroupChat(laneKey("signal:g:AbC+/=", "x"))).toBe(true);
  expect(isGroupChat(laneKey("wa:123@g.us", "x"))).toBe(true);
  expect(isGroupChat(laneKey("wa:123@s.whatsapp.net", "x"))).toBe(false);
});

test("a lane sends to its chat, and !more there pages its overflow", async () => {
  to.length = 0;
  await sendTo("lane:me#trading", "hi");
  expect(to).toEqual(["lane:me"]);

  await sendReply("lane:me#trading", "x".repeat(200));
  expect(to.every((k) => k === "lane:me")).toBe(true);
  expect(overflowSize("lane:me")).toBeGreaterThan(0);
  expect(takeOverflow("lane:me").length).toBeGreaterThan(0);
});
