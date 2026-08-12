"""Tests for ClaudeSession output parsing, stats, and session persistence."""

import json
import types

import pytest

from teleclaude.session_cli import ClaudeSession


def _result(stdout: str, returncode: int = 0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


@pytest.fixture
def session(tmp_path):
    return ClaudeSession(
        project_dir=str(tmp_path),
        session_file=str(tmp_path / "logs" / "claude_session.txt"),
    )


def test_parse_json(session):
    payload = {
        "result": "hello",
        "session_id": "abc123",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {"opus": {"contextWindow": 200000}},
    }
    text, sid, meta = session._parse_json(_result(json.dumps(payload)))
    assert (text, sid) == ("hello", "abc123")
    assert meta["input_tokens"] == 10
    assert meta["context_window"] == 200000


def test_parse_json_falls_back_to_raw_stdout(session):
    text, sid, meta = session._parse_json(_result("not json at all"))
    assert text == "not json at all"
    assert sid == "" and meta == {}


def test_parse_stream_json_prefers_result_line(session):
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "part one"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "part two"}]}},
        {"type": "result", "result": "final answer", "session_id": "s1", "usage": {}},
    ]
    text, sid, _meta = session._parse_stream_json(_result("\n".join(json.dumps(x) for x in lines)))
    assert text == "final answer"
    assert sid == "s1"


def test_parse_stream_json_joins_all_text_blocks_when_result_empty(session):
    # Claude splits one logical response across blocks; taking only the last
    # block silently truncated the reply.
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "part one"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "part two"}]}},
        {"type": "result", "result": "", "session_id": "s1", "usage": {}},
    ]
    text, _sid, _meta = session._parse_stream_json(_result("\n".join(json.dumps(x) for x in lines)))
    assert text == "part one\n\npart two"


def test_parse_stream_json_skips_malformed_lines(session):
    stdout = "\n".join([
        "{not json}",
        json.dumps({"type": "result", "result": "ok", "session_id": "s2", "usage": {}}),
        "",
    ])
    text, sid, _meta = session._parse_stream_json(_result(stdout))
    assert (text, sid) == ("ok", "s2")


def test_context_pct_and_trend(session):
    session._update_stats({"input_tokens": 10_000, "context_window": 100_000})
    assert session.context_pct == pytest.approx(10.0)
    session._update_stats({"input_tokens": 30_000, "context_window": 100_000})
    assert session.context_pct == pytest.approx(30.0)
    arrow, delta = session.context_trend
    assert arrow == "↑" and delta == pytest.approx(20.0)
    assert session.stats.peak_context_pct == pytest.approx(30.0)


def test_context_pct_none_without_data(session):
    assert session.context_pct is None
    assert session.est_turns_remaining is None


def test_compaction_detected_on_sharp_drop(session):
    session._update_stats({"input_tokens": 90_000, "context_window": 100_000})
    session._update_stats({"input_tokens": 20_000, "context_window": 100_000})
    assert session.stats.last_compaction_from == pytest.approx(90.0)


def test_pin_clear_roundtrip(session, tmp_path):
    session.pin("pinned-id")
    assert session.pinned_session_id == "pinned-id"
    assert (tmp_path / "logs" / "claude_session.txt").read_text() == "pinned-id"
    session.clear()
    assert session.session_id is None
    assert not (tmp_path / "logs" / "claude_session.txt").exists()


def test_set_model_rejects_unknown_and_persists_valid(session, tmp_path):
    assert session.set_model("sonnet") is True
    assert session.model == "sonnet"
    assert session.set_model("gpt") is False
    assert session.model == "sonnet"
    # A fresh session over the same dir picks the persisted model back up.
    reloaded = ClaudeSession(
        project_dir=str(tmp_path),
        session_file=str(tmp_path / "logs" / "claude_session.txt"),
    )
    assert reloaded.model == "sonnet"


def test_next_session_name_increments(tmp_path):
    s = ClaudeSession(
        project_dir=str(tmp_path),
        session_file=str(tmp_path / "logs" / "claude_session.txt"),
        session_name_prefix="bot",
    )
    assert s.next_session_name() == "bot-1"
    assert s.next_session_name() == "bot-2"
