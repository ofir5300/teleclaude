"""/context check, on-demand availability polling, and persistent usage-limit watcher."""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# One-turn ping used by every availability check. Haiku keeps the probe cheap.
_PROBE_CMD = [
    "claude", "--print", "--output-format", "json", "--max-turns", "1",
    "--model", "haiku", "-p", "hi",
]


class AvailabilityMixin:
    """Three flavors of Claude availability tracking:

    - /context: one-shot check
    - context polling: short-lived, auto-started on rate-limit
    - watcher: long-lived, user-toggled, persistent across restarts
    """

    # -- shared probe ------------------------------------------------------

    def _run_probe(self, timeout: int = 30) -> subprocess.CompletedProcess:
        """Run the availability probe. Raises subprocess.TimeoutExpired on timeout."""
        env = {**os.environ}
        env.pop("CLAUDECODE", None)  # allow nested claude invocation
        return subprocess.run(
            _PROBE_CMD, capture_output=True, text=True,
            timeout=timeout, cwd=self._project_dir, env=env,
        )

    def _probe_status(self, tag: str, timeout: int = 30) -> tuple[bool | None, str]:
        """Probe Claude and render a Telegram-ready status line.

        Returns (available, html_status). `available` is None when the outcome is
        unknown (timeout / launch failure) rather than a definitive yes or no.
        """
        print(f"[{tag}] Checking Claude availability: {' '.join(_PROBE_CMD)}", flush=True)
        try:
            result = self._run_probe(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[{tag}] Claude timed out", flush=True)
            return None, "⏳ Claude Code <b>timed out</b> (may be rate-limited)."
        except Exception as e:
            print(f"[{tag}] Check failed: {e}", flush=True)
            return None, f"❌ Error checking: {str(e)[:200]}"

        if result.returncode == 0:
            print(f"[{tag}] Claude is available (exit=0)", flush=True)
            return True, "✅ Claude Code is <b>available</b>!"
        print(f"[{tag}] Claude unavailable (exit={result.returncode})", flush=True)
        stderr = (result.stderr or "").strip()[:300]
        return False, f"⏳ Claude Code <b>unavailable</b>.\n<code>{stderr}</code>"

    # -- /context (one-shot) ----------------------------------------------

    def _cmd_context(self):
        """Check if Claude Code is available (not rate-limited)."""
        self.send("🔍 Checking Claude Code availability...")
        threading.Thread(
            target=lambda: self.send(self._probe_status("context")[1]),
            daemon=True,
        ).start()

    # -- short-lived context polling --------------------------------------

    def _start_context_polling(self):
        """Start background polling for Claude Code availability (every 5min, max 12h)."""
        if self._context_polling:
            return

        self._context_polling = True
        print("[poll] Starting Claude availability polling (every 5min)", flush=True)

        def poll():
            poll_interval = 300
            max_duration = 12 * 3600
            start = time.time()

            while self._context_polling and (time.time() - start) < max_duration:
                time.sleep(poll_interval)
                if not self._context_polling:
                    break
                available, _status = self._probe_status("poll")
                if available:
                    print("[poll] Claude is back online!", flush=True)
                    self.send("🟢 <b>Claude Code is back online!</b> You can send messages now.")
                    break

            self._context_polling = False
            self._context_poll_thread = None

        self._context_poll_thread = threading.Thread(target=poll, daemon=True)
        self._context_poll_thread.start()

    # -- long-lived usage-limit watcher -----------------------------------

    def _watcher_cleanup_session(self, session_id: str):
        """Delete the throwaway session transcript the probe just created."""
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
        """Extract reset hint from Claude's limit message.

        Handles both 5-hour ('resets 6:50pm (Asia/Jerusalem)') and weekly
        ('resets May 13 6:50pm', 'resets Wednesday 6:50pm') forms by capturing
        everything after 'resets' up to a sentence terminator.
        """
        m = re.search(r"resets?\s+([^.\n;|]+)", text, re.IGNORECASE)
        if not m:
            return ""
        hint = m.group(1).strip().rstrip(",")
        return hint[:80]

    def _watcher_seconds_until_reset(self, hint: str) -> int | None:
        """Parse a clock time out of the hint into seconds-from-now.

        Searches rather than anchors, so weekly hints ('Wednesday 6:50pm') still
        yield a time. A weekday/date prefix is ignored: worst case we wake early,
        re-probe, and recompute. Returns None if no clock time is present.
        """
        if not hint:
            return None
        m = re.search(
            r"(\d{1,2})(?::(\d{2}))?\s*([apAP][mM])\s*(?:\(([^)]+)\))?",
            hint.strip(),
        )
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3).lower()
        tzname = (m.group(4) or "").strip()

        if hour > 12:
            return None
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        tz = None
        if tzname and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                tz = None

        now = datetime.now(tz) if tz else datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())

    def _watcher_probe(self) -> tuple[bool | None, str, str]:
        """Single probe. Returns (blocked, reset_hint, snippet).

        `blocked` is None when the probe could not determine anything (timeout or
        launch failure). Callers must not treat that as "available" — doing so
        fires a false "limit reset" ping on every flaky probe.
        """
        try:
            r = self._run_probe(timeout=60)
        except subprocess.TimeoutExpired:
            return None, "", "timeout"
        except Exception as e:
            return None, "", f"err: {e!r}"

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
        self._watcher_wake.clear()
        self._persist_watcher_state(True)
        print("[watcher] started (60m free / 15m blocked)", flush=True)

        def loop():
            interval_free = 60 * 60
            fallback_blocked = 15 * 60
            retry_unknown = 5 * 60  # probe failed — retry soon, state unchanged
            grace = 60  # seconds after reset before re-probing

            while self._watcher_enabled:
                blocked, reset_hint, snippet = self._watcher_probe()
                state = {True: "rate-limited", False: "available", None: "unknown"}[blocked]
                print(f"[watcher] {state}: {snippet}", flush=True)

                if blocked is True and self._watcher_prev_blocked is not True:
                    msg = "🛑 <b>Claude usage limit reached</b> (rate-limited)."
                    if reset_hint:
                        msg += f"\n⏰ Resets: <b>{reset_hint}</b>"
                    msg += "\nI'll notify you when it resets."
                    self.send(msg)
                    self._watcher_last_reset_hint = reset_hint
                elif blocked is False and self._watcher_prev_blocked is True:
                    msg = "🟢 <b>Usage limit reset</b> — Claude is available again."
                    if self._watcher_last_reset_hint:
                        msg += f"\n<i>(was: resets {self._watcher_last_reset_hint})</i>"
                    self.send(msg)
                    print("[watcher] notified rate-limited -> available", flush=True)
                    self._watcher_last_reset_hint = ""

                if blocked is not None:
                    self._watcher_prev_blocked = blocked

                # Next sleep: unknown -> short retry, free -> 60m, blocked ->
                # until the parsed reset time (else a 15m fallback poll).
                if blocked is None:
                    wait = retry_unknown
                elif not blocked:
                    wait = interval_free
                else:
                    secs = self._watcher_seconds_until_reset(reset_hint or self._watcher_last_reset_hint)
                    wait = (secs + grace) if secs is not None else fallback_blocked
                print(f"[watcher] next probe in {wait}s", flush=True)
                self._watcher_wake.wait(wait)  # returns early when the watcher is stopped

            print("[watcher] stopped", flush=True)
            self._watcher_thread = None

        self._watcher_thread = threading.Thread(target=loop, daemon=True)
        self._watcher_thread.start()

    def _stop_watcher(self):
        self._watcher_enabled = False
        self._watcher_wake.set()  # break the sleep immediately
        self._persist_watcher_state(False)
