import { expect, test } from "bun:test";
import { isImageProcessingFailure } from "../src/signal.ts";

test("the two shapes this build's missing ImageIO actually takes", () => {
  // Both seen live against signal-cli 0.14.7 native: by file path, and by a
  // data URI that declares image/jpeg.
  expect(
    isImageProcessingFailure(
      new Error(
        "Can't load library: awt | java.library.path = [/usr/lib64, /lib64, /lib, /usr/lib] (UnsatisfiedLinkError)",
      ),
    ),
  ).toBe(true);
  expect(
    isImageProcessingFailure(
      new Error("Could not initialize class javax.imageio.ImageIO (NoClassDefFoundError)"),
    ),
  ).toBe(true);
});

test("a real failure is not retried into a second delivery", () => {
  for (const msg of [
    "signal-cli socket not connected",
    "Failed to send message: rate limited",
    "Untrusted identity key",
    "",
  ])
    expect(isImageProcessingFailure(new Error(msg))).toBe(false);
});
