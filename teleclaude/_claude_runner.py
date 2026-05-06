"""Claude plan/approve/reject lifecycle for free-text messages."""

import subprocess
import threading

from teleclaude._telegram import _escape_html


class ClaudeRunnerMixin:
    """Plan-mode → /approve implementation flow."""

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

    def _cmd_reject(self):
        """Reject the pending Claude plan."""
        if self._claude_pending_prompt:
            self._claude_pending_prompt = None
            self.send("🚫 Plan rejected.")
        else:
            self.send("ℹ️ No pending plan to reject.")
