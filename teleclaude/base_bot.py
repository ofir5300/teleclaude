"""Telegram bot base class with Claude Code plan/approve/reject workflow.

Sync implementation using raw requests (no python-telegram-bot dependency).
Subclass TeleClaudeBot and override hooks to add domain-specific logic.
"""

import html as _html
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from teleclaude.session_cli import ClaudeSession, VALID_MODELS
from teleclaude.self_update import restart

log = logging.getLogger(__name__)

# Telegram message length limit
_TG_MAX_LEN = 4000


def _escape_html(text: str) -> str:
    """Escape <, >, & so raw markdown/code doesn't break Telegram HTML parse_mode."""
    return _html.escape(text, quote=False)


class TeleClaudeBot:
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
        domain_commands()           - Register domain-specific /commands
        on_domain_callback(data, message_id) - Handle domain-specific callbacks
        help_text()                 - Customize /help message
        on_restart()                - Customize restart behavior
        plan_prompt_wrapper(text)   - Customize the plan-mode prompt
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
        self._seen_update_ids: set[int] = set()  # dedup Telegram re-deliveries

        self._last_message_text = ""
        self._project_dir = project_dir or str(Path.cwd())

        # Claude Code integration
        self.claude = claude_session or ClaudeSession(project_dir=self._project_dir)
        self._claude_pending_prompt: str | None = None
        self._claude_busy = False

        # Context polling state
        self._context_polling = False
        self._context_poll_thread = None

        # Continuous availability watcher (toggleable from /claude menu).
        # Probes every 60m while free, 15m while blocked; alerts on blocked->free transitions.
        # Toggle state persists across restarts via ~/.teleclaude/watcher_enabled.
        self._watcher_state_file = Path.home() / ".teleclaude" / "watcher_enabled"
        self._watcher_enabled = False
        self._watcher_thread = None
        self._watcher_prev_blocked: bool | None = None
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

    # -- Telegram HTTP methods ---------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str) -> bool:
        """Send a message. Tries HTML parse_mode first; falls back to plain text."""
        if not self.is_configured:
            return False
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            resp = __import__("requests").post(url, data=data, timeout=10)
            if resp.status_code == 200:
                return True
            # HTML parse failed — retry without parse_mode
            print(f"[!] Telegram HTML send failed ({resp.status_code}), retrying as plain text", flush=True)
            data.pop("parse_mode")
            resp2 = __import__("requests").post(url, data=data, timeout=10)
            if resp2.status_code != 200:
                print(f"[!] Telegram plain send also failed ({resp2.status_code}): {resp2.text[:300]}", flush=True)
            return resp2.status_code == 200
        except Exception as e:
            print(f"[!] Telegram send failed: {e}", flush=True)
            return False

    def send_long(self, message: str, max_len: int = _TG_MAX_LEN) -> bool:
        """Send a long message, splitting into multiple messages at newline boundaries."""
        if len(message) <= max_len:
            return self.send(message)

        chunks = []
        remaining = message
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, max_len)
            if cut <= 0:
                cut = max_len
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")

        ok = True
        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(0.3)
            if not self.send(chunk):
                ok = False
        return ok

    def send_with_markup(self, message: str, reply_markup: dict) -> Optional[int]:
        """Send a message with an inline keyboard. Returns message_id on success."""
        if not self.is_configured:
            return None
        try:
            import requests as _requests

            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": json.dumps(reply_markup),
            }
            resp = _requests.post(url, data=payload, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id")
            print(f"[!] Telegram send_with_markup failed ({resp.status_code}): {resp.text[:300]}", flush=True)
        except Exception as e:
            print(f"[!] Telegram send_with_markup failed: {e}", flush=True)
        return None

    def edit_message(self, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message in-place (for inline keyboard drill-down)."""
        if not self.is_configured:
            return False
        try:
            import requests as _requests

            url = f"{self.base_url}/editMessageText"
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup)
            resp = _requests.post(url, data=payload, timeout=10)
            if resp.status_code != 200:
                print(f"[!] Telegram edit_message failed ({resp.status_code}): {resp.text[:300]}", flush=True)
            return resp.status_code == 200
        except Exception as e:
            print(f"[!] Telegram edit_message failed: {e}", flush=True)
            return False

    def answer_callback_query(self, callback_query_id: str) -> bool:
        """Acknowledge a callback query (removes the loading spinner on the button)."""
        try:
            import requests as _requests

            url = f"{self.base_url}/answerCallbackQuery"
            resp = _requests.post(url, data={"callback_query_id": callback_query_id}, timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def get_updates(self, timeout: int = 30) -> list:
        """Get new messages from Telegram via long-polling."""
        try:
            import requests as _requests

            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            }
            resp = _requests.get(url, params=params, timeout=timeout + 5)
            if resp.status_code == 200:
                return resp.json().get("result", [])
        except (ConnectionResetError, ConnectionError) as e:
            # Transient network reset — normal for long-poll connections, just retry
            time.sleep(2)
        except Exception as e:
            print(f"[!] Failed to get updates: {e}")
            time.sleep(2)
        return []

    # -- Polling infrastructure --------------------------------------------

    def start_polling(self):
        """Start listening for commands in a background daemon thread."""
        self.running = True
        self._register_commands()

        def poll_loop():
            print("[i] Telegram command listener started", flush=True)
            while self.running:
                try:
                    updates = self.get_updates(timeout=30)
                    if updates:
                        print(f"[i] Telegram: {len(updates)} update(s) received", flush=True)
                    for update in updates:
                        try:
                            self.process_update(update)
                        except Exception as e:
                            print(f"[!] Telegram command error: {e}", flush=True)
                            try:
                                self.send(f"❌ Command error: {e}")
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[!] Telegram polling error: {e}", flush=True)
                    time.sleep(5)

        thread = threading.Thread(target=poll_loop, daemon=True)
        thread.start()
        return thread

    def stop_polling(self):
        """Stop listening for commands."""
        self.running = False

    def process_update(self, update: dict):
        """Process a single update (message or callback_query)."""
        uid = update.get("update_id")
        if uid is not None:
            if uid in self._seen_update_ids:
                return  # Telegram re-delivery — skip
            self._seen_update_ids.add(uid)
            # Keep the set bounded; discard IDs older than last 200
            if len(self._seen_update_ids) > 200:
                self._seen_update_ids.discard(min(self._seen_update_ids))
            self.last_update_id = uid

        # Handle inline button presses
        callback = update.get("callback_query")
        if callback:
            cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            if cb_chat_id == self.chat_id:
                self._handle_callback(callback)
            return

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))

        # Only process messages from authorized chat
        if chat_id != self.chat_id:
            return

        # Handle voice/audio messages
        voice = message.get("voice") or message.get("audio")
        if voice:
            self._handle_voice_message(voice.get("file_id"))
            return

        text = message.get("text", "").strip()
        if not text:
            return  # Sticker, photo, reaction — ignore

        # Store full message text for argument parsing
        self._last_message_text = text

        # Extract command or forward to Claude Code
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in self.commands:
                print(f"[cmd] {cmd} from user", flush=True)
                self.commands[cmd]()
            else:
                print(f"[cmd] Unknown command: {cmd}", flush=True)
                self.send(f"❓ Unknown command: {cmd}\nType /help for available commands")
        else:
            print(f"[claude] Free text received: \"{text[:80]}{'...' if len(text) > 80 else ''}\"", flush=True)
            self._handle_claude_message(text)

    def _handle_callback(self, callback: dict):
        """Handle inline keyboard button presses."""
        callback_id = callback.get("id", "")
        data = callback.get("data", "")
        message_id = callback.get("message", {}).get("message_id")
        print(f"[cb] Button pressed: {data}", flush=True)

        self.answer_callback_query(callback_id)

        if not message_id:
            return

        # Claude menu callbacks handled by base class
        if data.startswith("claude:"):
            self._handle_claude_callback(data, message_id)
            return

        # Domain-specific callbacks handled by subclass
        self.on_domain_callback(data, message_id)

    def _register_commands(self):
        """Register bot commands with Telegram (updates BotFather menu)."""
        # Only register /claude + /help at root; other Claude commands
        # are accessible via the /claude inline keyboard menu.
        bot_commands = [
            {"command": "claude", "description": "Claude Code menu"},
            {"command": "help", "description": "Show help message"},
        ]
        for cmd, (_handler, desc) in self.domain_commands().items():
            bot_commands.append({"command": cmd.lstrip("/"), "description": desc})

        try:
            import requests as _requests

            # Clear commands at all scopes — BotFather or prior code may have set
            # empty overrides at narrower scopes (e.g. all_private_chats) which
            # hide the default-scope commands from the Telegram menu.
            for scope in [
                None,  # default scope
                {"type": "all_private_chats"},
                {"type": "all_group_chats"},
                {"type": "all_chat_administrators"},
            ]:
                payload = {"scope": scope} if scope else {}
                dr = _requests.post(f"{self.base_url}/deleteMyCommands", json=payload, timeout=10)
                label = scope["type"] if scope else "default"
                print(f"[cmd-reg] deleteMyCommands({label}): {dr.status_code}", flush=True)

            url = f"{self.base_url}/setMyCommands"
            resp = _requests.post(url, json={"commands": bot_commands}, timeout=10)
            print(f"[cmd-reg] setMyCommands: {resp.status_code} {resp.json()}", flush=True)

            # Verify what Telegram actually has
            get_resp = _requests.post(f"{self.base_url}/getMyCommands", timeout=10)
            registered = get_resp.json()
            print(f"[cmd-reg] getMyCommands: {len(registered.get('result', []))} commands: {registered}", flush=True)

            if resp.status_code == 200 and resp.json().get("ok"):
                print(f"[i] Telegram bot commands registered ({len(bot_commands)} commands)", flush=True)
            else:
                print(f"[!] Failed to register commands: {resp.status_code} {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"[!] Failed to register commands: {e}", flush=True)

    # -- Generic command handlers ------------------------------------------

    def _cmd_help(self):
        self.send(self.help_text())

    def _cmd_restart(self):
        self.send("🔄 Restarting...")
        self.on_restart()

    def _cmd_reject(self):
        """Reject the pending Claude plan."""
        if self._claude_pending_prompt:
            self._claude_pending_prompt = None
            self.send("🚫 Plan rejected.")
        else:
            self.send("ℹ️ No pending plan to reject.")

    # -- Claude Code integration -------------------------------------------

    def _claude_stats_footer(self) -> str:
        """Build a compact stats footer from the last Claude turn."""
        s = self.claude.stats
        if s.total_turns == 0:
            return ""
        parts = [f"💰 ${s.total_cost_usd:.3f}", f"⏱ {s.total_duration_ms / 1000:.1f}s"]
        pct = self.claude.context_pct
        if pct is not None:
            arrow, _ = self.claude.context_trend
            peak_str = f" (peak {s.peak_context_pct:.0f}%)" if s.peak_context_pct > pct + 1 else ""
            est = self.claude.est_turns_remaining
            est_str = f" ~{est} left" if est is not None else ""
            time_left = self.claude.est_time_remaining
            if time_left is not None and time_left > 0:
                if time_left >= 60:
                    est_str += f" ≈{time_left // 60}m"
                else:
                    est_str += f" ≈{time_left}s"
            parts.append(f"📊 {pct:.0f}% {arrow}{peak_str}{est_str}")
        footer = "\n\n<i>" + " · ".join(parts) + "</i>"
        if pct is not None and pct > 85:
            footer += "\n⚠️ <i>Context {:.0f}% full — consider flushing session</i>".format(pct)
        # Compaction alert (shown once)
        if s.last_compaction_from > 0:
            footer += f"\n🔄 <i>Context was auto-compacted ({s.last_compaction_from:.0f}% → {pct:.0f}%)</i>"
            s.last_compaction_from = 0.0
        return footer

    def _handle_claude_message(self, text: str):
        """Forward a free-text message to Claude Code for analysis/planning."""
        if self._claude_busy:
            print("[claude] Rejected — already busy", flush=True)
            self.send("⏳ Claude is still working on the previous request. Please wait.")
            return

        if self.claude.session_name:
            status = f"session {self.claude.session_name}"
        elif self.claude.session_id:
            status = f"session …{self.claude.session_id[:8]}"
        else:
            status = "new session"
        print(f"[claude] Sending to Claude (plan mode, {status})", flush=True)
        self.send(f"🧠 Asking Claude Code ({self.claude.model})... ({status})")
        self._claude_busy = True

        def run_claude():
            try:
                plan_prompt = self.plan_prompt_wrapper(text)
                # Bootstrap from .handoff.md is handled transparently by ClaudeSession
                # whenever a new session starts — no extra logic needed here.
                response = self.claude.run(plan_prompt, allow_edits=False, timeout=240)

                if response and not response.startswith("Error:"):
                    print(f"[claude] Plan received ({len(response)} chars)", flush=True)
                    self._claude_pending_prompt = text
                    self.send_long(f"🧠 <b>Claude's Plan:</b>\n\n{_escape_html(response)}")
                    self.send("👆 /approve to implement, /reject to cancel" + self._claude_stats_footer())
                elif response and response.startswith("Error:"):
                    print(f"[claude] Error: {response[:200]}", flush=True)
                    self.send(f"❌ {_escape_html(response)}")
                    self._claude_pending_prompt = None
                    # Auto-start context polling on rate limit
                    if any(kw in response.lower() for kw in ("rate", "limit", "capacity")):
                        self._start_context_polling()
                else:
                    print("[claude] Empty response from Claude", flush=True)
                    self.send("🧠 Claude returned an empty response. Try again or rephrase your question.")
                    self._claude_pending_prompt = None

            except subprocess.TimeoutExpired:
                print("[claude] Timed out (4min)", flush=True)
                self.send("⏰ Claude timed out (4min). Try a simpler request.")
                self._claude_pending_prompt = None
            except FileNotFoundError:
                print("[claude] CLI not found", flush=True)
                self.send("❌ <code>claude</code> CLI not found.")
                self._claude_pending_prompt = None
            except Exception as e:
                print(f"[claude] Exception: {e}", flush=True)
                self.send(f"❌ Claude error: {str(e)[:500]}")
                self._claude_pending_prompt = None
            finally:
                self._claude_busy = False

        thread = threading.Thread(target=run_claude, daemon=True)
        thread.start()

    def _cmd_approve(self):
        """Approve and implement Claude's pending plan."""
        if not self._claude_pending_prompt:
            self.send("ℹ️ No pending plan to approve. Send a message first.")
            return

        if self._claude_busy:
            self.send("⏳ Claude is already working.")
            return

        prompt = self._claude_pending_prompt
        self._claude_pending_prompt = None
        self._claude_busy = True
        print(f"[claude] Approved — implementing (edit mode)", flush=True)
        self.send("⚡ Implementing... Claude is writing code now.")

        def run_implementation():
            try:
                impl_prompt = (
                    f"The user APPROVED this plan via Telegram. Implement it now: {prompt}"
                )
                response = self.claude.run(impl_prompt, allow_edits=True, timeout=300)

                if response and not response.startswith("Error:"):
                    print(f"[claude] Implementation complete ({len(response)} chars)", flush=True)
                    self.send_long(f"✅ <b>Done!</b>\n\n{_escape_html(response)}")
                    footer = self._claude_stats_footer()
                    if footer:
                        self.send(footer)
                elif response and response.startswith("Error:"):
                    print(f"[claude] Implementation error: {response[:200]}", flush=True)
                    self.send(f"❌ {_escape_html(response)}")
                else:
                    print("[claude] Implementation complete (no output)", flush=True)
                    self.send("✅ Implementation complete (no output).")

            except subprocess.TimeoutExpired:
                print("[claude] Implementation timed out (5min)", flush=True)
                self.send("⏰ Implementation timed out (5 min limit).")
            except Exception as e:
                print(f"[claude] Implementation exception: {e}", flush=True)
                self.send(f"❌ Implementation error: {str(e)[:500]}")
            finally:
                self._claude_busy = False

        thread = threading.Thread(target=run_implementation, daemon=True)
        thread.start()

    # -- /claude interactive sub-menu --------------------------------------

    def _cmd_claude(self):
        """Show Claude Code interactive menu with inline keyboard."""
        text, keyboard = self._build_claude_menu()
        self.send_with_markup(text, keyboard)

    def _build_claude_menu(self):
        """Build the Claude Code main menu. Returns (text, keyboard)."""
        status_icon = "🔴" if self._claude_busy else "🟢"
        status_text = "Busy" if self._claude_busy else "Available"
        polling_text = " | 📡 Polling" if self._context_polling else ""

        if self.claude.session_name:
            sid_short = f"…{self.claude.pinned_session_id[:8]}" if self.claude.pinned_session_id else ""
            session_line = f"📌 Session: <b>{self.claude.session_name}</b>" + (f" (<code>{sid_short}</code>)" if sid_short else "")
        else:
            sid_short = f"…{self.claude.pinned_session_id[:8]}" if self.claude.pinned_session_id else "none"
            session_line = f"📌 Session: <code>{sid_short}</code>"

        pending_text = ""
        if self._claude_pending_prompt:
            snippet = self._claude_pending_prompt[:50]
            pending_text = f"\n📋 Pending plan: <i>{snippet}...</i>"

        # Model + compact stats line
        model_line = f"\n🤖 Model: <b>{self.claude.model}</b>"
        stats_line = ""
        s = self.claude.stats
        if s.total_turns > 0:
            pct = self.claude.context_pct
            parts = [f"💰 ${s.total_cost_usd:.3f}", f"🔄 {s.total_turns}"]
            if pct is not None:
                filled = int(pct / 10)
                bar = "█" * filled + "░" * (10 - filled)
                arrow, _ = self.claude.context_trend
                warn = " ⚠️" if pct > 80 else ""
                peak_str = f" pk:{s.peak_context_pct:.0f}%" if s.peak_context_pct > pct + 1 else ""
                parts.append(f"📊 [{bar}] {pct:.0f}% {arrow}{peak_str}{warn}")
            stats_line = "\n" + " | ".join(parts)

        msg = (
            f"<b>🧠 Claude Code</b> (CLI subprocess)\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"{status_icon} Status: <b>{status_text}</b>{polling_text}\n"
            f"{session_line}"
            f"{model_line}"
            f"{stats_line}"
            f"{pending_text}"
        )

        buttons = []
        buttons.append([{"text": "🔍 Check Availability", "callback_data": "claude:check"}])

        if self._claude_pending_prompt:
            buttons.append([
                {"text": "✅ Approve Plan", "callback_data": "claude:approve"},
                {"text": "🚫 Reject Plan", "callback_data": "claude:reject"},
            ])

        # Model switcher row
        model_buttons = []
        for m in VALID_MODELS:
            icon = "◉" if self.claude.model == m else "○"
            model_buttons.append({"text": f"{icon} {m.title()}", "callback_data": f"claude:model_{m}"})
        buttons.append(model_buttons)

        buttons.append([{"text": "📌 Session Info", "callback_data": "claude:session"}])
        buttons.append([{"text": "🔄 Flush & New Session", "callback_data": "claude:flush"}])
        buttons.append([{"text": "⚡ Restart Bot", "callback_data": "claude:restart"}])

        if self._context_polling:
            buttons.append([{"text": "⏹ Stop Polling", "callback_data": "claude:poll_stop"}])

        watcher_label = (
            "🔔 Usage-limit watcher: ON" if self._watcher_enabled
            else "🔕 Usage-limit watcher: OFF"
        )
        buttons.append([{"text": watcher_label, "callback_data": "claude:watcher_toggle"}])

        keyboard = {"inline_keyboard": buttons}
        return msg.strip(), keyboard

    def _build_claude_session_view(self):
        """Build session detail view. Returns (text, keyboard)."""
        pinned = self.claude.pinned_session_id
        active = self.claude.session_id

        msg = "<b>📌 Claude Code Session</b>\n\n"
        if self.claude.session_name:
            msg += f"🏷 Name: <b>{self.claude.session_name}</b>\n"
        if pinned:
            msg += f"📌 Pinned (SSOT): <code>{pinned}</code>\n"
        else:
            msg += "📌 Pinned: <i>none</i>\n"
        if active and active != pinned:
            msg += f"🔄 Active: <code>{active}</code>\n"
        msg += f"🤖 Model: <b>{self.claude.model}</b>\n"

        # Session stats
        s = self.claude.stats
        if s.total_turns > 0:
            msg += f"\n<b>📊 Session Stats</b>\n"
            msg += f"🔄 Turns: {s.total_turns}\n"
            msg += f"💰 Cost: ${s.total_cost_usd:.4f}\n"
            msg += f"⏱ Duration: {s.total_duration_ms / 1000:.1f}s\n"
            msg += f"📥 In: {s.total_input_tokens:,}  📤 Out: {s.total_output_tokens:,}\n"
            msg += f"💾 Cache: {s.total_cache_read_tokens:,} read / {s.total_cache_creation_tokens:,} created\n"
            pct = self.claude.context_pct
            if pct is not None:
                msg += f"\n<b>📊 Context Window</b>\n"
                filled = int(pct / 10)
                bar = "█" * filled + "░" * (10 - filled)
                warn = " ⚠️" if pct > 80 else ""
                msg += f"├ Current: [{bar}] {pct:.0f}%{warn}\n"
                if s.peak_context_pct > pct + 1:
                    pfilled = int(s.peak_context_pct / 10)
                    pbar = "█" * pfilled + "░" * (10 - pfilled)
                    msg += f"├ Peak:    [{pbar}] {s.peak_context_pct:.0f}%\n"
                arrow, delta = self.claude.context_trend
                if len(s.context_history) >= 2:
                    msg += f"├ Trend:   {arrow} ({delta:+.0f}% last turn)\n"
                avg_g = self.claude.avg_growth_per_turn
                est_label = "~" if len(s.context_history) < 2 else ""
                msg += f"├ Avg growth: {est_label}{avg_g:.1f}%/turn\n"
                est = self.claude.est_turns_remaining
                if est is not None:
                    time_left = self.claude.est_time_remaining
                    time_str = ""
                    if time_left is not None and time_left > 0:
                        if time_left >= 60:
                            time_str = f" ≈ {time_left // 60}m"
                        else:
                            time_str = f" ≈ {time_left}s"
                    msg += f"└ Remaining: ~{est} turns{time_str}\n"
                elif pct > 80:
                    msg += f"└ ⚠️ Consider flushing session\n"

        msg += "\n<i>To pin a new session, send:</i>\n<code>/session pin &lt;session_id&gt;</code>"

        buttons = [
            [{"text": "🗑 Clear Session", "callback_data": "claude:session_clear"}],
            [{"text": "⬅ Back to Claude Menu", "callback_data": "claude:menu"}],
        ]
        keyboard = {"inline_keyboard": buttons}
        return msg.strip(), keyboard

    def _handle_claude_callback(self, data: str, message_id: int):
        """Handle claude:* inline button presses."""

        if data == "claude:menu":
            text, keyboard = self._build_claude_menu()
            self.edit_message(message_id, text, keyboard)

        elif data == "claude:check":
            self.edit_message(message_id, "🔍 Checking Claude Code availability...")

            def check_and_update():
                try:
                    env = {**os.environ}
                    env.pop("CLAUDECODE", None)
                    result = subprocess.run(
                        ["claude", "--print", "--output-format", "json", "--max-turns", "1",
                         "-p", "Reply with exactly: ok"],
                        capture_output=True, text=True, timeout=30,
                        cwd=self._project_dir, env=env,
                    )
                    if result.returncode == 0:
                        status = "✅ Claude Code is <b>available</b>!"
                    else:
                        stderr = (result.stderr or "").strip()[:200]
                        status = f"⏳ Claude Code <b>unavailable</b>\n<code>{stderr}</code>"
                except subprocess.TimeoutExpired:
                    status = "⏳ Claude Code <b>timed out</b> (may be rate-limited)"
                except Exception as e:
                    status = f"❌ Error: {str(e)[:200]}"

                keyboard = {"inline_keyboard": [[{"text": "⬅ Back to Claude Menu", "callback_data": "claude:menu"}]]}
                self.edit_message(message_id, status, keyboard)

            threading.Thread(target=check_and_update, daemon=True).start()

        elif data == "claude:approve":
            if not self._claude_pending_prompt:
                keyboard = {"inline_keyboard": [[{"text": "⬅ Back", "callback_data": "claude:menu"}]]}
                self.edit_message(message_id, "ℹ️ No pending plan to approve.", keyboard)
                return
            self.edit_message(message_id, "⚡ Implementing... Claude is writing code now.")
            self._cmd_approve()

        elif data == "claude:reject":
            self._claude_pending_prompt = None
            text, keyboard = self._build_claude_menu()
            self.edit_message(message_id, "🚫 Plan rejected.\n\n" + text, keyboard)

        elif data == "claude:session":
            text, keyboard = self._build_claude_session_view()
            self.edit_message(message_id, text, keyboard)

        elif data == "claude:session_clear":
            self.claude.clear()
            text, keyboard = self._build_claude_menu()
            self.edit_message(message_id, "🗑 Session cleared.\n\n" + text, keyboard)

        elif data == "claude:watcher_toggle":
            if self._watcher_enabled:
                self._stop_watcher()
                self.send("🔕 <b>Usage-limit watcher disabled.</b>")
            else:
                self._start_watcher()
                self.send(
                    "🔔 <b>Usage-limit watcher enabled.</b>\n"
                    "Probes Claude every <b>60m</b> while available, <b>15m</b> while rate-limited.\n"
                    "I'll ping when your 5-hour usage window resets."
                )
            text, keyboard = self._build_claude_menu()
            self.edit_message(message_id, text, keyboard)
            return

        elif data == "claude:poll_stop":
            self._context_polling = False
            text, keyboard = self._build_claude_menu()
            self.edit_message(message_id, text, keyboard)

        elif data.startswith("claude:model_"):
            model = data.split("_", 1)[1]
            if self.claude.set_model(model):
                text, keyboard = self._build_claude_menu()
                self.edit_message(message_id, f"🤖 Model → <b>{model}</b>\n\n" + text, keyboard)
            else:
                text, keyboard = self._build_claude_menu()
                self.edit_message(message_id, f"❌ Unknown model: {model}\n\n" + text, keyboard)

        elif data == "claude:restart":
            self.edit_message(message_id, "⚡ Restarting bot...")
            self._cmd_restart()

        elif data == "claude:flush":
            self.edit_message(message_id, "🔄 Flushing session... generating summary from current session.")
            self._flush_and_new_session(message_id)

    def _flush_and_new_session(self, message_id: int):
        """Flush current session context via /handoff write, then unpin."""
        handoff_write_prompt = (
            "Run /handoff write now. Write the handoff file to .handoff.md in the project root. "
            "Be specific and concrete — a fresh session with zero context must act on this file alone."
        )

        def do_flush():
            try:
                self.edit_message(message_id, "🔄 Step 1/2: Writing session handoff...")

                result = self.claude.run(handoff_write_prompt, allow_edits=True, timeout=240)

                handoff_path = Path(self._project_dir) / ".handoff.md"
                if not handoff_path.exists():
                    back_kb = {"inline_keyboard": [[{"text": "⬅ Back", "callback_data": "claude:menu"}]]}
                    self.edit_message(message_id, f"❌ Handoff write failed — .handoff.md not created.\n{(result or '')[:300]}", back_kb)
                    return

                # Capture stats before clearing
                s = self.claude.stats
                recap = ""
                if s.total_turns > 0:
                    recap = f"\n📊 Session recap: {s.total_turns} turns · ${s.total_cost_usd:.3f} · {s.total_duration_ms / 1000:.1f}s"

                self.edit_message(message_id, "🔄 Step 2/2: Clearing session pin...")
                self.claude.clear()  # ClaudeSession auto-bootstraps from .handoff.md on next message

                back_kb = {"inline_keyboard": [[{"text": "⬅ Claude Menu", "callback_data": "claude:menu"}]]}
                self.edit_message(
                    message_id,
                    f"✅ <b>Session flushed!</b>\n\n"
                    f"📝 Context saved to <code>.handoff.md</code>\n"
                    f"🔄 Next message will start a new session with handoff context"
                    f"{recap}",
                    back_kb,
                )

            except Exception as e:
                back_kb = {"inline_keyboard": [[{"text": "⬅ Back", "callback_data": "claude:menu"}]]}
                self.edit_message(message_id, f"❌ Flush error: {str(e)[:300]}", back_kb)

        threading.Thread(target=do_flush, daemon=True).start()

    # -- /session command --------------------------------------------------

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

        # Show session info
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

    # -- /context and availability polling ---------------------------------

    def _cmd_context(self):
        """Check if Claude Code is available (not rate-limited)."""
        print("[context] Checking Claude availability: claude --print -p 'Reply with exactly: ok'", flush=True)
        self.send("🔍 Checking Claude Code availability...")

        def check():
            try:
                env = {**os.environ}
                env.pop("CLAUDECODE", None)
                result = subprocess.run(
                    ["claude", "--print", "--output-format", "json", "--max-turns", "1",
                     "-p", "Reply with exactly: ok"],
                    capture_output=True, text=True, timeout=30,
                    cwd=self._project_dir, env=env,
                )
                if result.returncode == 0:
                    print("[context] Claude is available (exit=0)", flush=True)
                    self.send("✅ Claude Code is available!")
                else:
                    print(f"[context] Claude unavailable (exit={result.returncode})", flush=True)
                    stderr = (result.stderr or "").strip()[:300]
                    self.send(f"⏳ Claude Code unavailable.\n<code>{stderr}</code>")
            except subprocess.TimeoutExpired:
                print("[context] Claude timed out", flush=True)
                self.send("⏳ Claude Code timed out (may be rate-limited).")
            except Exception as e:
                print(f"[context] Check failed: {e}", flush=True)
                self.send(f"❌ Error checking: {str(e)[:200]}")

        threading.Thread(target=check, daemon=True).start()

    def _start_context_polling(self):
        """Start background polling for Claude Code availability."""
        if self._context_polling:
            return

        self._context_polling = True
        print("[poll] Starting Claude availability polling (every 5min)", flush=True)

        def poll():
            poll_interval = 300  # 5 minutes
            max_duration = 12 * 3600  # 12 hours
            start = time.time()

            while self._context_polling and (time.time() - start) < max_duration:
                time.sleep(poll_interval)
                if not self._context_polling:
                    break
                try:
                    print("[poll] Checking Claude availability: claude --print -p 'Reply with exactly: ok'", flush=True)
                    env = {**os.environ}
                    env.pop("CLAUDECODE", None)
                    result = subprocess.run(
                        ["claude", "--print", "--output-format", "json", "--max-turns", "1",
                         "-p", "Reply with exactly: ok"],
                        capture_output=True, text=True, timeout=30,
                        cwd=self._project_dir, env=env,
                    )
                    if result.returncode == 0:
                        print("[poll] Claude is back online!", flush=True)
                        self.send("🟢 <b>Claude Code is back online!</b> You can send messages now.")
                        self._context_polling = False
                        break
                    else:
                        print(f"[poll] Still unavailable (exit={result.returncode})", flush=True)
                except Exception as e:
                    print(f"[poll] Check failed: {e}", flush=True)

            self._context_polling = False
            self._context_poll_thread = None

        self._context_poll_thread = threading.Thread(target=poll, daemon=True)
        self._context_poll_thread.start()

    # -- Continuous availability watcher -----------------------------------

    def _watcher_cleanup_session(self, session_id: str):
        if not session_id:
            return
        root = Path.home() / ".claude" / "projects"
        if not root.is_dir():
            return
        for p in root.rglob(f"{session_id}.jsonl"):
            try:
                p.unlink()
            except OSError:
                pass

    def _watcher_parse_reset(self, text: str) -> str:
        """Extract reset time from Claude's limit message, e.g. 'resets 6:50pm (Asia/Jerusalem)'."""
        import re
        m = re.search(r"resets?\s+([0-9:apm\s]+(?:\([^)]+\))?)", text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _watcher_probe(self) -> tuple[bool, str, str]:
        """Single probe. Returns (blocked, reset_hint, snippet)."""
        try:
            env = {**os.environ}
            env.pop("CLAUDECODE", None)
            r = subprocess.run(
                ["claude", "--print", "--output-format", "json", "--max-turns", "1",
                 "--model", "haiku", "-p", "hi"],
                capture_output=True, text=True, timeout=60,
                cwd=self._project_dir, env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "", "timeout"
        except Exception as e:
            return False, "", f"err: {e!r}"

        session_id = ""
        api_err = None
        is_err = False
        result_text = ""
        try:
            data = json.loads(r.stdout)
            session_id = data.get("session_id", "") or ""
            api_err = data.get("api_error_status")
            is_err = bool(data.get("is_error"))
            result_text = (data.get("result") or "")[:160]
        except (json.JSONDecodeError, ValueError):
            pass

        self._watcher_cleanup_session(session_id)

        blocked = api_err == 429 or (is_err and "limit" in result_text.lower())
        reset_hint = self._watcher_parse_reset(result_text) if blocked else ""
        return blocked, reset_hint, f"api_err={api_err} is_err={is_err} :: {result_text}"

    def _persist_watcher_state(self, enabled: bool):
        try:
            self._watcher_state_file.parent.mkdir(parents=True, exist_ok=True)
            self._watcher_state_file.write_text("1" if enabled else "0")
        except OSError as e:
            print(f"[watcher] failed to persist state: {e!r}", flush=True)

    def _start_watcher(self):
        if self._watcher_enabled:
            return
        self._watcher_enabled = True
        self._watcher_prev_blocked = None
        self._persist_watcher_state(True)
        print("[watcher] started (60m free / 15m blocked)", flush=True)

        def loop():
            interval_blocked = 15 * 60
            interval_free = 60 * 60
            last_reset_hint = ""
            while self._watcher_enabled:
                try:
                    blocked, reset_hint, snippet = self._watcher_probe()
                    state = "rate-limited" if blocked else "available"
                    print(f"[watcher] {state}: {snippet}", flush=True)

                    # First time we see a block, surface the reset time once.
                    if blocked and self._watcher_prev_blocked is not True:
                        msg = "🛑 <b>Claude usage limit reached</b> (rate-limited)."
                        if reset_hint:
                            msg += f"\n⏰ Resets: <b>{reset_hint}</b>"
                        msg += "\n🔁 Polling every 15m until the window resets."
                        self.send(msg)
                        last_reset_hint = reset_hint

                    if self._watcher_prev_blocked is True and not blocked:
                        msg = "🟢 <b>Usage limit reset</b> — Claude is available again."
                        if last_reset_hint:
                            msg += f"\n<i>(was: resets {last_reset_hint})</i>"
                        self.send(msg)
                        print("[watcher] notified rate-limited -> available", flush=True)
                        last_reset_hint = ""

                    self._watcher_prev_blocked = blocked
                except Exception as e:
                    print(f"[watcher] probe error: {e!r}", flush=True)
                # Sleep in 5s slices so toggle-off takes effect quickly
                wait = interval_blocked if self._watcher_prev_blocked else interval_free
                slept = 0
                while self._watcher_enabled and slept < wait:
                    time.sleep(5)
                    slept += 5
            print("[watcher] stopped", flush=True)
            self._watcher_thread = None

        self._watcher_thread = threading.Thread(target=loop, daemon=True)
        self._watcher_thread.start()

    def _stop_watcher(self):
        self._watcher_enabled = False
        self._persist_watcher_state(False)

    # -- Voice message handling --------------------------------------------

    def _handle_voice_message(self, file_id: str):
        """Download and transcribe a Telegram voice message, then route to Claude."""
        if not file_id:
            self.send("Could not process voice message.")
            return

        self.send("Transcribing voice message...")

        def transcribe():
            try:
                import requests as _requests

                # Download the voice file from Telegram
                file_info = _requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}).json()
                file_path = file_info.get("result", {}).get("file_path", "")
                if not file_path:
                    self.send("Failed to get voice file from Telegram.")
                    return

                download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
                audio_data = _requests.get(download_url).content

                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    tmp.write(audio_data)
                    tmp_path = tmp.name

                # Lazy-load whisper model
                if self._whisper_model is None:
                    import whisper

                    self._whisper_model = whisper.load_model("base")

                result = self._whisper_model.transcribe(tmp_path)
                text = result.get("text", "").strip()

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

                if not text:
                    self.send("Could not transcribe audio (empty result).")
                    return

                self.send(f"Heard: <i>{text}</i>")
                self._handle_claude_message(text)

            except ImportError:
                self.send("whisper not installed. Run: pip install openai-whisper")
            except Exception as e:
                self.send(f"Transcription error: {str(e)[:500]}")

        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()
