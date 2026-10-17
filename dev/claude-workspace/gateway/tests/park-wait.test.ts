import { afterEach, expect, test } from "bun:test";
import { parkWaitMs } from "../src/claude.ts";
import { config } from "../src/config.ts";

const saved = { ...config.background };
afterEach(() => Object.assign(config.background, saved));

const bash = { task_id: "b1", task_type: "local_bash", description: "ninja" };
const agent = { task_id: "a1", task_type: "local_agent", description: "port chunk 2" };

test("a command alone gets the ordinary park", () => {
  expect(parkWaitMs([bash])).toBe(config.background.waitMs);
});

test("any agent among the tasks gets the agent park", () => {
  expect(parkWaitMs([bash, agent])).toBe(config.background.agentWaitMs);
  expect(parkWaitMs([{ task_type: "local_workflow" }])).toBe(
    config.background.agentWaitMs,
  );
});

test("an untyped task is treated as a command", () => {
  expect(parkWaitMs([{ description: "?" }])).toBe(config.background.waitMs);
});

test("the agent park never shortens the ordinary one", () => {
  Object.assign(config.background, { waitMs: 7_200_000, agentWaitMs: 60_000 });
  expect(parkWaitMs([agent])).toBe(7_200_000);
});

test("parking switched off stays off for agents too", () => {
  Object.assign(config.background, { waitMs: 0 });
  expect(parkWaitMs([agent])).toBe(0);
});
