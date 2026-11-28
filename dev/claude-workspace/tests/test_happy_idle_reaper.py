import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("happy_idle_reaper", ROOT / "recovery" / "happy-idle-reaper.py")
reaper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reaper)

DAY = 86400
NOW = 1_800_000_000


def record(sid, **overrides):
    value = {"sessionId": sid, "container": "happy-daemon", "state": "running", "busy": False,
             "pid": 4242, "metadata": {"claudeSessionId": f"claude-{sid}"}}
    value.update(overrides)
    return value


class SelectIdleTests(unittest.TestCase):
    def select(self, records, activity, daemon=None):
        daemon = daemon if daemon is not None else {r["sessionId"]: 1 for r in records}
        return [sid for sid, _ in reaper.select_idle(records, daemon, activity, NOW, 3 * DAY)]

    def test_only_idle_past_threshold(self):
        ages = {"claude-old": 4 * DAY, "claude-new": 2 * DAY}
        records = [record("old"), record("new")]
        self.assertEqual(self.select(records, lambda m: NOW - ages[m["claudeSessionId"]]), ["old"])

    def test_busy_stopped_foreign_and_untracked_are_kept(self):
        records = [record("busy", busy=True), record("stopped", state="stopped"),
                   record("term", container="term"), record("untracked")]
        daemon = {"busy": 1, "stopped": 1, "term": 1}
        self.assertEqual(self.select(records, lambda m: NOW - 30 * DAY, daemon), [])

    def test_unknown_transcript_is_never_guessed(self):
        self.assertEqual(self.select([record("x")], lambda m: None), [])

    def test_oldest_first(self):
        ages = {"claude-a": 5 * DAY, "claude-b": 9 * DAY}
        records = [record("a"), record("b")]
        self.assertEqual(self.select(records, lambda m: NOW - ages[m["claudeSessionId"]]), ["b", "a"])


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)

    def touch(self, rel, mtime):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        os.utime(path, (mtime, mtime))

    def test_claude_subagent_writes_count_as_activity(self):
        self.touch(".claude/projects/p/abc.jsonl", NOW - 10 * DAY)
        self.touch(".claude/projects/p/abc/subagents/agent-1.jsonl", NOW - DAY)
        self.assertEqual(reaper.last_activity(self.home, {"claudeSessionId": "abc"}), NOW - DAY)

    def test_codex_rollout(self):
        self.touch(".codex/sessions/2026/11/01/rollout-2026-11-01-th1.jsonl", NOW - 5 * DAY)
        self.assertEqual(reaper.last_activity(self.home, {"codexThreadId": "th1"}), NOW - 5 * DAY)

    def test_missing(self):
        self.assertIsNone(reaper.last_activity(self.home, {"claudeSessionId": "nope"}))
        self.assertIsNone(reaper.last_activity(self.home, {}))


class MarkStoppedTests(unittest.TestCase):
    def test_dead_running_record_becomes_stopped_live_one_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "live").mkdir()
            dead = record("dead", pid=2 ** 22 + 7)  # beyond pid_max: never alive
            alive = record("alive", pid=os.getpid())
            (root / "live/dead.json").write_text(json.dumps(dead))
            (root / "live/alive.json").write_text(json.dumps(alive))
            reaper.mark_stopped(root, "dead", dead["pid"])
            reaper.mark_stopped(root, "alive", alive["pid"])
            self.assertEqual(json.loads((root / "live/dead.json").read_text())["state"], "stopped")
            self.assertEqual(json.loads((root / "live/alive.json").read_text())["state"], "running")


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_stop(self):
        calls = []
        original = reaper.daemon_post
        reaper.daemon_post = lambda port, path, body: (
            calls.append(path) or {"children": [{"happySessionId": "old", "pid": 1}]})
        self.addCleanup(setattr, reaper, "daemon_post", original)
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / ".happy/workspace-recovery/live").mkdir(parents=True)
            (home / ".happy/daemon.state.json").write_text(json.dumps({"httpPort": 1}))
            (home / ".happy/workspace-recovery/live/old.json").write_text(json.dumps(record("old")))
            transcript = home / ".claude/projects/p/claude-old.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}")
            os.utime(transcript, (time.time() - 9 * DAY,) * 2)
            chosen = reaper.sweep(home, home / ".happy/workspace-recovery", 3 * DAY, True, log=lambda m: None)
        self.assertEqual([sid for sid, _ in chosen], ["old"])
        self.assertEqual(calls, ["/list"])


if __name__ == "__main__":
    unittest.main()
