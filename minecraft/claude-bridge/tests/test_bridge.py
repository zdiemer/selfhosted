"""Tests for the stream parser and chat formatting.

The multi-message path is the interesting one: `claude -p` emits an assistant
event per message and then repeats the last one in the terminal `result`
event, so the parser has to deliver messages as they arrive without letting
that trailing repeat double-post in chat.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

import bridge


def _assistant(*blocks: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def _text(t: str) -> str:
    return _assistant({"type": "text", "text": t})


def _tool(name: str, **inp: object) -> str:
    return _assistant({"type": "tool_use", "name": name, "input": inp})


def _result(text: str, session_id: str = "sess-1", is_error: bool = False) -> str:
    return json.dumps({"type": "result", "result": text,
                       "session_id": session_id, "is_error": is_error})


# ---------------------------------------------------------------------------
# _consume_events
# ---------------------------------------------------------------------------
def test_streams_each_assistant_message():
    seen: list[str] = []
    res = bridge._consume_events(
        [_text("on it — checking your chests"),
         _tool("read_container", pos="10 64 -3"),
         _text("found 47 iron"),
         _result("found 47 iron")],
        on_message=seen.append, max_messages=4)

    assert seen == ["on it — checking your chests", "found 47 iron"]
    assert res.messages_sent == 2
    assert res.session_id == "sess-1"
    assert res.dropped == 0
    # The trailing result repeats the last message — callers use this to
    # suppress the double-post.
    assert res.final_text == res.last_seen_text == "found 47 iron"


def test_tool_use_drives_progress_not_messages():
    messages: list[str] = []
    progress: list[str] = []
    bridge._consume_events([_tool("teleport_caller_home"), _result("done")],
                           on_message=messages.append,
                           on_progress=progress.append, max_messages=4)

    assert messages == []
    assert len(progress) == 1


def test_message_cap_suppresses_extras_but_keeps_final_answer():
    seen: list[str] = []
    res = bridge._consume_events(
        [_text("one"), _text("two"), _text("three"), _result("three")],
        on_message=seen.append, max_messages=2)

    assert seen == ["one", "two"]
    assert res.messages_sent == 2
    assert res.dropped == 1
    # Still reported, so the caller can post the answer that got dropped.
    assert res.final_text == "three"


def test_no_on_message_keeps_one_shot_behavior():
    res = bridge._consume_events([_text("hi"), _result("hi")], max_messages=1)
    assert res.messages_sent == 0
    assert res.final_text == "hi"


def test_blank_and_malformed_lines_are_skipped():
    seen: list[str] = []
    res = bridge._consume_events(
        ["", "   ", "not json at all", _text("  spaced  "), _result("spaced")],
        on_message=seen.append, max_messages=4)

    assert seen == ["spaced"]
    assert res.final_text == "spaced"


def test_empty_text_blocks_are_not_sent():
    seen: list[str] = []
    bridge._consume_events([_text(""), _text("   "), _result("")],
                           on_message=seen.append, max_messages=4)
    assert seen == []


def test_result_error_is_flagged():
    res = bridge._consume_events([_result("", is_error=True)], max_messages=1)
    assert res.had_error is True


def test_stops_at_result():
    seen: list[str] = []
    bridge._consume_events([_text("first"), _result("first"), _text("after")],
                           on_message=seen.append, max_messages=4)
    assert seen == ["first"]


def test_on_message_failure_does_not_abort_the_turn():
    def boom(_: str) -> None:
        raise RuntimeError("rcon down")

    res = bridge._consume_events([_text("a"), _result("a")],
                                 on_message=boom, max_messages=4)
    assert res.messages_sent == 0
    assert res.final_text == "a"


# ---------------------------------------------------------------------------
# Claude._ask_locked — dedupe, session write-back, timeout
# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self.stdin = io.StringIO()
        self.stdout = iter(f"{line}\n" for line in lines)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def claude(tmp_path, monkeypatch):
    sessions = bridge.SessionStore(tmp_path / "sessions.json")
    c = bridge.Claude(sessions, "sys", "sonnet", timeout=30)

    def run(lines, **kwargs):
        proc = FakeProc(lines, **kwargs)
        monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: proc)
        return proc

    c._run = run  # type: ignore[attr-defined]
    return c


def test_streamed_reply_is_not_posted_twice(claude):
    claude._run([_text("checking…"), _text("47 iron"), _result("47 iron")])
    seen: list[str] = []
    turn = claude.ask("uuid-1", "StarFoxA", "how much iron do I have",
                      on_message=seen.append, max_messages=4)

    assert seen == ["checking…", "47 iron"]
    assert turn.messages_sent == 2
    assert turn.text == ""          # nothing left for the caller to post
    assert claude.sessions.get("uuid-1") == "sess-1"


def test_capped_turn_still_posts_the_answer(claude):
    claude._run([_text("one"), _text("two"), _result("two")])
    seen: list[str] = []
    turn = claude.ask("uuid-1", "StarFoxA", "q", on_message=seen.append,
                      max_messages=1)

    assert seen == ["one"]
    assert turn.text == "two"


def test_one_shot_caller_gets_the_text(claude):
    claude._run([_text("wear a helmet"), _result("wear a helmet")])
    turn = claude.ask("uuid-1", "StarFoxA", "I died", max_messages=1,
                      ephemeral=True)

    assert turn.text == "wear a helmet"
    assert turn.messages_sent == 0
    # ephemeral turns never write back a session id
    assert claude.sessions.get("uuid-1") is None


def test_empty_stream_reports_no_reply(claude):
    claude._run([])
    turn = claude.ask("uuid-1", "StarFoxA", "q")
    assert turn.text == "(no reply)"


def test_crash_with_no_output_reports_an_error(claude):
    claude._run([], returncode=1, stderr="boom")
    turn = claude.ask("uuid-1", "StarFoxA", "q")
    assert turn.text == "(sorry, I hit an error)"


def test_bad_session_is_cleared_on_crash(claude):
    claude.sessions.set("uuid-1", "stale-session")
    claude._run([], returncode=1, stderr="No conversation found with session ID")
    claude.ask("uuid-1", "StarFoxA", "q")
    assert claude.sessions.get("uuid-1") == ""


def test_timeout_with_nothing_streamed_tells_the_player(claude, monkeypatch):
    claude.timeout = 0.05

    class SilentProc(FakeProc):
        """A subprocess that emits nothing and never exits on its own —
        the case the old in-loop deadline check couldn't catch."""

        def __init__(self):
            super().__init__([])
            self._killed = threading.Event()
            self.stdout = self._never()

        def _never(self):
            self._killed.wait(10)
            return
            yield  # pragma: no cover - makes this a generator

        def kill(self):
            self.killed = True
            self._killed.set()

    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: SilentProc())
    turn = claude.ask("uuid-1", "StarFoxA", "q")
    assert turn.text == "(timed out — try again with a shorter question)"


# ---------------------------------------------------------------------------
# Chat formatting
# ---------------------------------------------------------------------------
def test_chunk_splits_on_whitespace():
    chunks = list(bridge._chunk("word " * 100, 40))
    assert all(len(c) <= 40 for c in chunks)
    assert " ".join(chunks).split() == ("word " * 100).split()


def test_markdown_subset_renders_components():
    out = bridge._markdown_to_components("you need **64** `iron_ingot` *ish*")
    assert {"text": "64", "color": "white", "bold": True} in out
    assert {"text": "iron_ingot", "color": "aqua"} in out
    assert {"text": "ish", "color": "gray", "italic": True} in out


def test_unmatched_delimiter_stays_literal():
    out = bridge._markdown_to_components("2 * 3 iron")
    assert "".join(c["text"] for c in out) == "2 * 3 iron"
