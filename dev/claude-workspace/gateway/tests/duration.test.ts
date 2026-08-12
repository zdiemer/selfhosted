import { expect, test } from "bun:test";
import { formatDuration, parseDuration } from "../src/router.ts";

test("durations are typed the way a phone types them", () => {
  expect(parseDuration("30m")).toBe(30 * 60_000);
  expect(parseDuration("2h")).toBe(2 * 3_600_000);
  expect(parseDuration("90")).toBe(90 * 60_000); // bare number = minutes
  expect(parseDuration("1 hour")).toBe(3_600_000);
  expect(parseDuration("45 mins")).toBe(45 * 60_000);
});

test("non-durations are rejected rather than guessed at", () => {
  for (const bad of ["", "soon", "on", "off", "0m", "-5m", "abc"])
    expect(parseDuration(bad)).toBeNull();
});

test("more than a day of unattended root is refused", () => {
  // This pod is cluster-admin with root on every node; "24h" is already the
  // outer edge of a time box and anything past it is just !auto on.
  expect(parseDuration("24h")).toBe(86_400_000);
  expect(parseDuration("25h")).toBeNull();
  expect(parseDuration("2000m")).toBeNull();
});

test("remaining time reads as a duration", () => {
  expect(formatDuration(30 * 60_000)).toBe("30m");
  expect(formatDuration(3_600_000)).toBe("1h");
  expect(formatDuration(80 * 60_000)).toBe("1h20m");
  // Never "0m left" while it is still in force.
  expect(formatDuration(3_000)).toBe("1m");
});
