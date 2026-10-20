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

RESUME_NEEDLE = """      persistExtendedHistory: true
    };
    const result = await this.request("thread/resume", params);"""
RESUME_REPLACEMENT = """      persistExtendedHistory: true,
      // workspace-recovery-resume-v1: Happy does not consume returned turns.
      excludeTurns: true
    };
    const result = await this.request("thread/resume", params);"""

READY_NEEDLE = """    opts.session.updateMetadata((currentMetadata) => ({
      ...currentMetadata,
      codexThreadId: resumedThread.threadId
    }));"""
READY_REPLACEMENT = READY_NEEDLE + """
    // workspace-recovery-ready-v1: acknowledgment requires a resumed agent.
    opts.session.workspaceRecoveryReady = true;
    process.emit("workspace-recovery-ready", opts.session.sessionId);"""


def patch(root):
    root = pathlib.Path(root)
    version = json.loads((root / "package.json").read_text())["version"]
    if version != "1.2.5":
        raise RuntimeError(f"Happy {version}: review recovery adapter before upgrading")
    files = list((root / "dist").glob("types-*.mjs")) + list((root / "dist").glob("types-*.cjs"))
    changes = {}
    for file in files:
        source = file.read_text()
        for needle, replacement in [(NEEDLE, REPLACEMENT), (RESUME_NEEDLE, RESUME_REPLACEMENT)]:
            if replacement in source:
                continue
            if source.count(needle) != 1:
                raise RuntimeError(f"Happy recovery adapter mismatch in {file.name}")
            source = source.replace(needle, replacement)
        changes[file] = source
    if len(changes) != 2:
        raise RuntimeError(f"expected both ESM/CJS Happy message hooks, found {len(changes)}")
    ready_files = 0
    for file in list((root / "dist").glob("index-*.mjs")) + list((root / "dist").glob("index-*.cjs")):
        source = file.read_text()
        if READY_REPLACEMENT in source:
            ready_files += 1
        elif source.count(READY_NEEDLE) == 1:
            changes[file] = source.replace(READY_NEEDLE, READY_REPLACEMENT)
            ready_files += 1
    if ready_files != 2:
        raise RuntimeError(f"expected both ESM/CJS Happy readiness hooks, found {ready_files}")
    for file, source in changes.items():
        file.write_text(source)


if __name__ == "__main__":
    patch(sys.argv[1])
