"""/claude inline-keyboard menu and session view."""

import os
import subprocess
import threading
from pathlib import Path

from teleclaude.session_cli import VALID_MODELS


class ClaudeMenuMixin:
    """All UI for the /claude menu and session drilldown."""

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

                s = self.claude.stats
                recap = ""
                if s.total_turns > 0:
                    recap = f"\n📊 Session recap: {s.total_turns} turns · ${s.total_cost_usd:.3f} · {s.total_duration_ms / 1000:.1f}s"

                self.edit_message(message_id, "🔄 Step 2/2: Clearing session pin...")
                self.claude.clear()

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
