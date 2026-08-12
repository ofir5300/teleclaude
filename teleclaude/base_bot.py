"""Telegram bot base class with Claude Code plan/approve/reject workflow.

Sync implementation using raw requests (no python-telegram-bot dependency).
Subclass TeleClaudeBot and override hooks to add domain-specific logic.

Implementation is split across mixins for readability:
    _telegram.py        — HTTP API (send/edit/get_updates)
    _polling.py         — Long-poll loop + update dispatch
    _claude_runner.py   — Plan/approve/reject lifecycle
    _claude_menu.py     — /claude inline-keyboard UI
    _availability.py    — /context, polling, usage-limit watcher
    _voice.py           — Voice → Whisper → Claude
"""

import os
import re
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from teleclaude._availability import AvailabilityMixin
from teleclaude._claude_menu import ClaudeMenuMixin
from teleclaude._claude_runner import ClaudeRunnerMixin
from teleclaude._polling import PollingMixin
from teleclaude._telegram import TelegramMixin, _request_with_retry
from teleclaude._voice import VoiceMixin
from teleclaude.self_update import restart
from teleclaude.session_cli import ClaudeSession

SEEN_UPDATE_IDS_MAX = 200


class TeleClaudeBot(
    TelegramMixin,
    PollingMixin,
    ClaudeRunnerMixin,
    ClaudeMenuMixin,
    AvailabilityMixin,
    VoiceMixin,
):
    """Base Telegram bot with built-in Claude Code integration.

    Built-in commands:
        /claude   - Claude Code menu (session, approve/reject, flush)
        /session  - Session management (pin, clear)
        /context  - Check Claude availability
        /approve  - Execute Claude's pending plan
        /reject   - Discard pending plan
        /restart  - Restart the bot process
        /help     - Show commands

    Free-text messages are automatically routed to Claude in read-only mode.
    Voice messages are transcribed via Whisper and routed to Claude.

    Subclass and override:
        domain_commands()                    - Register domain-specific /commands
        on_domain_callback(data, message_id) - Handle domain-specific callbacks
        help_text()                          - Customize /help message
        on_restart()                         - Customize restart behavior
        plan_prompt_wrapper(text)            - Customize the plan-mode prompt
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        claude_session: ClaudeSession | None = None,
        project_dir: str | None = None,
    ):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self.running = False
        # Dedup Telegram re-deliveries. Set for O(1) lookup, deque for O(1) eviction
        # of the oldest id once the window is full.
        self._seen_update_ids: set[int] = set()
        self._seen_update_order: deque[int] = deque(maxlen=SEEN_UPDATE_IDS_MAX)

        self._last_message_text = ""
        self._project_dir = project_dir or str(Path.cwd())

        # Claude Code integration
        self.claude = claude_session or ClaudeSession(project_dir=self._project_dir)
        self._claude_pending_prompt: str | None = None
        self._claude_busy = False

        # Context polling state (short-lived, auto-started on rate-limit)
        self._context_polling = False
        self._context_poll_thread = None

        # Continuous availability watcher (toggleable from /claude menu).
        # Alerts on rate-limited -> available transitions; persists across restarts.
        # State is scoped per chat_id so multiple teleclaude bots on the same machine
        # don't share toggle state, double-probe the API, or send duplicate pings.
        _scope = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.chat_id) or "default")
        self._watcher_state_file = Path.home() / ".teleclaude" / f"watcher_{_scope}"
        self._watcher_enabled = False
        self._watcher_thread = None
        self._watcher_prev_blocked: bool | None = None
        self._watcher_last_reset_hint: str = ""
        self._watcher_wake = threading.Event()  # set to interrupt the watcher's sleep
        if self._watcher_state_file.exists():
            try:
                if self._watcher_state_file.read_text().strip() == "1":
                    # Defer actual start until after __init__ finishes (thread can call self.send)
                    threading.Timer(2.0, self._start_watcher).start()
                    print("[watcher] auto-resuming from persisted state", flush=True)
            except OSError:
                pass

        # Whisper model (lazy-loaded on first voice message)
        self._whisper_model = None

        # Build command registry: built-in + domain commands
        self.commands: dict[str, Callable] = {
            "/claude": self._cmd_claude,
            "/session": self._cmd_session,
            "/context": self._cmd_context,
            "/approve": self._cmd_approve,
            "/reject": self._cmd_reject,
            "/restart": self._cmd_restart,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }
        for cmd, (handler, _desc) in self.domain_commands().items():
            self.commands[cmd] = handler

    # -- Extensibility hooks (override in subclass) ------------------------

    def domain_commands(self) -> dict[str, tuple[Callable, str]]:
        """Return {"/cmd": (handler, "description")} for domain-specific commands.

        Example:
            return {
                "/status": (self._cmd_status, "Portfolio status"),
                "/scan": (self._cmd_scan, "Find opportunities"),
            }
        """
        return {}

    def on_domain_callback(self, data: str, message_id: int) -> bool:
        """Handle domain-specific callback_query data. Return True if handled."""
        return False

    def help_text(self) -> str:
        """Override to customize /help output."""
        lines = [
            "<b>Commands</b>",
            "",
            "<b>Claude Code</b>",
            "/claude - Claude Code menu",
            "/session - Session management",
            "/context - Check availability",
            "/approve - Approve pending action",
            "/reject - Reject pending action",
            "/restart - Restart the bot",
            "/help - This message",
        ]
        domain = self.domain_commands()
        if domain:
            lines.append("")
            for cmd, (_handler, desc) in domain.items():
                lines.append(f"{cmd} - {desc}")
        lines.append("")
        lines.append("Send any free text to chat with Claude Code.")
        return "\n".join(lines)

    def on_restart(self):
        """Override to customize restart behavior. Default: os.execv restart."""
        restart()

    def plan_prompt_wrapper(self, user_text: str) -> str:
        """Override to customize the prompt sent to Claude in plan mode."""
        return (
            f"The user sent this via Telegram: {user_text}\n\n"
            "Analyze the request and respond concisely. "
            "If a code change is needed, describe your plan (under 3000 chars). "
            "They will send /approve to let you implement it."
        )

    # -- Generic command handlers ------------------------------------------

    def _cmd_help(self):
        self.send(self.help_text())

    def _cmd_restart(self):
        self.send("🔄 Restarting...")
        # Ack the current update batch before execv. Otherwise, if /restart was
        # triggered via a callback button, Telegram has not yet seen the
        # advanced offset (the polling loop never gets back to call getUpdates
        # after the handler returns). After execv, last_update_id resets to 0,
        # the same callback is redelivered, and the bot enters an infinite
        # restart loop under any supervisor that respawns the process.
        try:
            _request_with_retry(
                "GET",
                f"{self.base_url}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 0},
                timeout=5,
            )
        except Exception as e:
            print(f"[!] pre-restart Telegram ack failed: {e}", flush=True)
        self.on_restart()

    def _cmd_session(self):
        """Show or manage the Claude Code session."""
        parts = self._last_message_text.split()
        if len(parts) >= 3 and parts[1].lower() == "pin":
            new_id = parts[2].strip()
            self.claude.pin(new_id)
            self.send(f"📌 Pinned session: <code>{new_id[:12]}…</code>")
            return
        if len(parts) >= 2 and parts[1].lower() == "clear":
            self.claude.clear()
            self.send("🗑 Session cleared. Next message starts fresh.")
            return

        pinned = self.claude.pinned_session_id
        active = self.claude.session_id
        name = self.claude.session_name
        msg = "<b>🧠 Claude Code Session</b>\n\n"
        if name:
            msg += f"🏷 Name: <b>{name}</b>\n"
        if pinned:
            msg += f"📌 Pinned (SSOT): <code>{pinned}</code>\n"
        else:
            msg += "📌 Pinned: <i>none</i>\n"
        if active and active != pinned:
            msg += f"🔄 Active: <code>{active}</code>\n"
        msg += "\n<b>Commands:</b>"
        msg += "\n<code>/session pin &lt;session_id&gt;</code> — pin a session"
        msg += "\n<code>/session clear</code> — start fresh"
        self.send(msg)
