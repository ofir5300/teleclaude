"""Tests for message chunking and update dispatch (no network, no Claude CLI)."""

import pytest

from teleclaude import TeleClaudeBot
from teleclaude._telegram import _escape_html


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Keep watcher state out of the real ~/.teleclaude (and out of test runs)."""
    monkeypatch.setattr("teleclaude.base_bot.Path.home", lambda: tmp_path)


@pytest.fixture
def bot(tmp_path):
    b = TeleClaudeBot(token="t", chat_id="42", project_dir=str(tmp_path))
    b.sent: list[str] = []
    b.send = lambda msg: (b.sent.append(msg), True)[1]
    return b


def _message(text: str, chat_id: str = "42", update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_escape_html():
    assert _escape_html("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"


def test_send_long_splits_on_newline_boundaries(bot):
    body = "\n".join(f"line {i}" for i in range(500))
    assert bot.send_long(body, max_len=200) is True
    assert len(bot.sent) > 1
    assert all(len(c) <= 200 for c in bot.sent)
    assert "".join(c.replace("\n", "") for c in bot.sent) == body.replace("\n", "")


def test_send_long_passes_through_short_message(bot):
    assert bot.send_long("short") is True
    assert bot.sent == ["short"]


def test_send_long_hard_splits_when_no_newline(bot):
    assert bot.send_long("x" * 500, max_len=100) is True
    assert len(bot.sent) == 5


def test_unknown_command_replies(bot):
    bot.process_update(_message("/nope"))
    assert "Unknown command" in bot.sent[0]


def test_foreign_chat_is_ignored(bot):
    bot.process_update(_message("/help", chat_id="999"))
    assert bot.sent == []


def test_duplicate_update_id_processed_once(bot):
    bot.process_update(_message("/help", update_id=7))
    bot.process_update(_message("/help", update_id=7))
    assert len(bot.sent) == 1


def test_seen_update_window_is_bounded(bot):
    from teleclaude.base_bot import SEEN_UPDATE_IDS_MAX

    for uid in range(SEEN_UPDATE_IDS_MAX + 50):
        bot.process_update(_message("/help", update_id=uid))
    assert len(bot._seen_update_ids) == SEEN_UPDATE_IDS_MAX
    assert bot.last_update_id == SEEN_UPDATE_IDS_MAX + 49


def test_domain_commands_are_registered(tmp_path):
    class MyBot(TeleClaudeBot):
        def domain_commands(self):
            return {"/status": (lambda: None, "Show status")}

    b = MyBot(token="t", chat_id="42", project_dir=str(tmp_path))
    assert "/status" in b.commands
    assert "/status - Show status" in b.help_text()


def test_reject_without_pending_plan(bot):
    bot._cmd_reject()
    assert "No pending plan" in bot.sent[0]
