import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch as mock_patch
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "recovery" / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


recovery = module("happy_recovery", "happy-recovery.py")
patch = module("patch_happy", "patch-happy.py")


class HappyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.record = {
            "sessionId": "happy-original", "container": "happy-daemon",
            "metadata": {"path": str(self.root), "flavor": "claude", "claudeSessionId": "claude-original"},
            "state": "running", "busy": True, "updatedAt": time.time() * 1000,
        }
        self.saved = {
            "encryptionKey": "fixture-key-never-printed", "encryptionVariant": "dataKey",
            "metadataVersion": 2, "agentStateVersion": 3, "seq": 4,
        }

    def test_native_claude_reconnect_keeps_both_ids(self):
        args, cwd, env = recovery.launch_spec(self.record, self.saved, "token", {})
        self.assertIn("claude-original", args)
        self.assertIn("--happy-starting-mode", args)
        self.assertEqual(cwd, str(self.root))
        self.assertEqual(env["HAPPY_RECONNECT_SESSION_ID"], "happy-original")
        self.assertEqual(env["HAPPY_WORKSPACE_RESUME_PROMPT"], recovery.PROMPT)
        self.assertNotIn("fixture-key-never-printed", " ".join(args))

    def test_codex_uses_thread_id_and_preserves_known_settings(self):
        self.record["metadata"] = {"path": str(self.root), "flavor": "codex", "codexThreadId": "codex-original"}
        self.record["settings"] = {"permissionMode": "auto", "model": "existing-model"}
        args, _, env = recovery.launch_spec(self.record, self.saved, "token", {})
        self.assertEqual(args[:4], ["happy", "codex", "--resume", "codex-original"])
        self.assertIn("auto", args)
        self.assertEqual(json.loads(env["HAPPY_WORKSPACE_SETTINGS"])["model"], "existing-model")

    def test_idle_session_reconnects_without_inventing_work(self):
        self.record["busy"] = False
        _, _, env = recovery.launch_spec(self.record, self.saved, "token", {})
        self.assertEqual(env["HAPPY_WORKSPACE_RESUME_PROMPT"], "")

    def test_server_metadata_supplies_missing_thread_and_overrides_stale_path(self):
        metadata = {"path": str(self.root), "flavor": "codex", "codexThreadId": "actual-thread"}
        record = {**self.record, "metadata": {"path": "/stale", "flavor": "codex"}}
        with mock_patch.object(recovery.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"metadata": metadata}))) as run:
            record, saved = recovery.refresh_metadata(record, self.saved)
        args, cwd, _ = recovery.launch_spec(record, saved, "token", {})
        self.assertIn("actual-thread", args)
        self.assertEqual(cwd, str(self.root))
        self.assertTrue(record["busy"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_metadata_lookup_fails_closed_and_retains_archive_state(self):
        for response in [subprocess.CompletedProcess([], 1, ""),
                         subprocess.CompletedProcess([], 0, "not json")]:
            with mock_patch.object(recovery.subprocess, "run", return_value=response):
                with self.assertRaises(ValueError):
                    recovery.refresh_metadata(self.record, self.saved)
        with mock_patch.object(recovery.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, '{"metadata":{"lifecycleState":"archived"}}')):
            record, saved = recovery.refresh_metadata(self.record, self.saved)
        self.assertEqual(record["metadata"]["lifecycleState"], "archived")
        self.assertEqual(saved["metadata"]["lifecycleState"], "archived")

    def test_missing_transcript_or_key_does_not_start_fresh(self):
        for record, saved in [(self.record, {}), ({**self.record, "metadata": {"flavor": "claude"}}, self.saved)]:
            with self.assertRaises(ValueError):
                recovery.launch_spec(record, saved, "token", {})

    def test_only_recent_live_records_for_this_container_are_recovered(self):
        records = [
            ("active", self.record),
            ("stopped", {**self.record, "state": "stopped"}),
            ("other", {**self.record, "container": "term"}),
            ("old", {**self.record, "updatedAt": 0}),
            ("alive", {**self.record, "pid": os.getpid(), "podUid": "pod", "identity": recovery.identity(os.getpid())}),
        ]
        for name, record in records:
            recovery.write(self.root / "live" / (name + ".json"), record)
        chosen = recovery.candidates(self.root, "happy-daemon", "pod", time.time() * 1000, 3600_000)
        self.assertEqual(chosen, [self.record])

    def test_pre_hook_upgrade_uses_only_the_explicit_restart_marker(self):
        home = self.root / "home"
        recovery.write(home / ".happy/restart-notice.json", {"at": time.time(), "sessions": "wanted"})
        recovery.write(home / ".happy/sessions.json", {"sessions": {
            "wanted": {**self.saved, "metadata": self.record["metadata"]},
            "stale-running": {**self.saved, "metadata": self.record["metadata"]},
        }})
        recovery.capture_legacy(self.root, home, "happy-daemon")
        self.assertEqual(list(recovery.read(self.root / "happy-daemon-pending.json")), ["wanted"])
        self.assertFalse(recovery.read(self.root / "happy-daemon-pending.json")["wanted"]["record"]["busy"])

    def test_patch_is_version_checked_and_idempotent(self):
        (self.root / "dist").mkdir()
        (self.root / "package.json").write_text('{"version":"1.2.5"}')
        for extension in ["mjs", "cjs"]:
            (self.root / "dist" / ("types-test." + extension)).write_text(
                patch.NEEDLE + "\n  }\n" + patch.RESUME_NEEDLE)
            (self.root / "dist" / ("index-test." + extension)).write_text(patch.READY_NEEDLE)
        patch.patch(self.root)
        patch.patch(self.root)
        self.assertIn("workspace-recovery-hook-v1", (self.root / "dist/types-test.mjs").read_text())
        self.assertIn("excludeTurns: true", (self.root / "dist/types-test.mjs").read_text())
        self.assertIn("workspace-recovery-ready-v1", (self.root / "dist/index-test.mjs").read_text())
        (self.root / "package.json").write_text('{"version":"future"}')
        with self.assertRaises(RuntimeError):
            patch.patch(self.root)


if __name__ == "__main__":
    unittest.main()
