import { expect, test } from "bun:test";
import { isWithinCwdRoots } from "../src/config.ts";

const ROOTS = ["/home/node/code/rachelfreeman"];

test("no roots configured means anywhere, as before", () => {
  // The unscoped instance must keep behaving exactly as it did; containment is
  // opt-in, and an empty GW_CWD_ROOTS is the common case.
  expect(isWithinCwdRoots("/etc", [])).toBe(true);
  expect(isWithinCwdRoots("/home/node/code/selfhosted", [])).toBe(true);
});

test("the root itself and anything under it is allowed", () => {
  expect(isWithinCwdRoots("/home/node/code/rachelfreeman", ROOTS)).toBe(true);
  expect(isWithinCwdRoots("/home/node/code/rachelfreeman/src", ROOTS)).toBe(
    true,
  );
});

test("paths outside the roots are refused", () => {
  for (const bad of [
    "/etc",
    "/home/node",
    "/home/node/.ssh",
    "/home/node/code",
    "/home/node/code/selfhosted",
  ])
    expect(isWithinCwdRoots(bad, ROOTS)).toBe(false);
});

test("a sibling that merely shares the prefix is not inside the root", () => {
  // The bug a bare startsWith would have: `-backup` is a different directory
  // that happens to begin with the same characters.
  expect(isWithinCwdRoots("/home/node/code/rachelfreeman-backup", ROOTS)).toBe(
    false,
  );
  expect(isWithinCwdRoots("/home/node/code/rachelfreemanX", ROOTS)).toBe(false);
});

test("relative segments cannot walk out of a root", () => {
  expect(
    isWithinCwdRoots("/home/node/code/rachelfreeman/../selfhosted", ROOTS),
  ).toBe(false);
  expect(isWithinCwdRoots("/home/node/code/rachelfreeman/../../.ssh", ROOTS)).toBe(
    false,
  );
  // ...and a walk that ends up back inside is fine.
  expect(
    isWithinCwdRoots("/home/node/code/rachelfreeman/src/../src", ROOTS),
  ).toBe(true);
});

test("multiple roots are each honoured", () => {
  const roots = [
    "/home/node/code/rachelfreeman",
    "/home/node/code/selfhosted/web/rachel-freeman",
  ];
  expect(isWithinCwdRoots("/home/node/code/rachelfreeman", roots)).toBe(true);
  expect(
    isWithinCwdRoots("/home/node/code/selfhosted/web/rachel-freeman", roots),
  ).toBe(true);
  expect(isWithinCwdRoots("/home/node/code/selfhosted/web", roots)).toBe(false);
  expect(
    isWithinCwdRoots("/home/node/code/selfhosted/web/whatnowgg", roots),
  ).toBe(false);
});
