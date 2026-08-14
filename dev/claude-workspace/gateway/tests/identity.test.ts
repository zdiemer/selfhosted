import { describe, expect, test } from "bun:test";
import {
  isUuid,
  signalAuthIds,
  unusableSignalEntries,
} from "../src/identity.ts";
import { matchesAllowlist } from "../src/router.ts";

const ACI = "8f0c1a52-6d4e-4a5b-9c31-2f7d5e8a1b03";

describe("isUuid", () => {
  test("an ACI is a UUID, a phone number never is", () => {
    expect(isUuid(ACI)).toBe(true);
    expect(isUuid(ACI.toUpperCase())).toBe(true);
    expect(isUuid("+15555550123")).toBe(false);
    expect(isUuid("15555550123")).toBe(false);
    expect(isUuid(undefined)).toBe(false);
    expect(isUuid("")).toBe(false);
  });
});

describe("signalAuthIds", () => {
  test("keeps the ACI and drops the number, wherever it sat", () => {
    expect(signalAuthIds(["+15555550123", ACI, "+15555550123"])).toEqual([ACI]);
    // `source` carries the number on some envelopes and the ACI on others, so
    // the filter is by shape rather than by position.
    expect(signalAuthIds([undefined, undefined, ACI])).toEqual([ACI]);
  });

  test("an envelope with no ACI authenticates nothing", () => {
    expect(signalAuthIds(["+15555550123"])).toEqual([]);
  });

  test("a number in the allowlist cannot admit a stranger's ACI", () => {
    const allowed = ["+15555550123"];
    // The number is shared by the sender, and matches the allowlist entry
    // verbatim — this is exactly the match that used to admit them.
    const envelope = ["+15555550123", "11111111-2222-3333-4444-555555555555"];
    expect(matchesAllowlist(envelope, allowed)).toBe(true);
    expect(matchesAllowlist(signalAuthIds(envelope), allowed)).toBe(false);
  });
});

describe("unusableSignalEntries", () => {
  test("reports the entries that can no longer match", () => {
    expect(unusableSignalEntries([ACI, "+15555550123"])).toEqual([
      "+15555550123",
    ]);
    expect(unusableSignalEntries([ACI])).toEqual([]);
  });
});
