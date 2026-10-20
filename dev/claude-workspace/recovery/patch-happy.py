#!/usr/bin/env python3
"""Small, version-checked hook at Happy's shared Claude/Codex message boundary.

Fail the image build on an incompatible upstream layout. Do not patch a live
installation: invoke against the npm install in the Docker build.
"""
import json
import pathlib
import sys

NEEDLE = """  onUserMessage(callback) {
    this.pendingMessageCallback = callback;"""
REPLACEMENT = """  onUserMessage(callback) {
    this.pendingMessageCallback = callback;
    // workspace-recovery-hook-v1: opt-in only in workspace containers.
    if (process.env.HAPPY_WORKSPACE_RECOVERY === "true") {
      import("/opt/workspace-recovery/happy-hook.mjs")
        .then(({ install }) => install(this, callback))
        .catch((error) => logger.warn("[workspace recovery] hook unavailable", error));
    }"""


def patch(root):
    root = pathlib.Path(root)
    version = json.loads((root / "package.json").read_text())["version"]
    if version != "1.2.5":
        raise RuntimeError(f"Happy {version}: review recovery adapter before upgrading")
    files = list((root / "dist").glob("types-*.mjs")) + list((root / "dist").glob("types-*.cjs"))
    changed = 0
    for file in files:
        source = file.read_text()
        if "workspace-recovery-hook-v1" in source:
            changed += 1
            continue
        if source.count(NEEDLE) == 1:
            file.write_text(source.replace(NEEDLE, REPLACEMENT))
            changed += 1
    if changed != 2:
        raise RuntimeError(f"expected both ESM/CJS Happy message hooks, found {changed}")


if __name__ == "__main__":
    patch(sys.argv[1])
