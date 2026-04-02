"""Claude Code subprocess wrapper with session persistence.

Two modes: plan (read-only analysis) and edit (file modifications).
Session IDs are persisted to disk and resumed with --resume for multi-turn context.
Supports named sessions, pinned SSOT semantics, stream-json parsing, and handoff bootstrap.
"""

import dataclasses
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

VALID_MODELS = ("opus", "sonnet", "haiku")


@dataclasses.dataclass
class SessionStats:
    """Cumulative usage stats for a Claude session."""

    total_turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    context_window: int = 0        # from latest turn's modelUsage
    last_input_tokens: int = 0     # latest turn's input + cache_read (context size)


# Default tools for each mode - callers can override
PLAN_TOOLS = ["Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "Agent"]
EDIT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Agent"]


class ClaudeSession:
    """Manages a persistent Claude Code CLI session with plan/edit modes.

    Usage:
        session = ClaudeSession(project_dir="/path/to/repo")
        plan = session.run("Analyze this code", allow_edits=False)
        result = session.run("Implement the plan", allow_edits=True)

    Features:
        - Session persistence with --resume
        - Plan/edit permission modes
        - Stream-json or json output format
        - Named sessions with auto-incrementing counter
        - Pinned SSOT session ID (never auto-overwritten)
        - Handoff bootstrap (.handoff.md)
        - Fallback callback when pinned session is unavailable
    """

    def __init__(
        self,
        project_dir: str,
        session_file: str | None = None,
        model: str = "opus",
        output_format: str = "json",
        verbose: bool = False,
        auto_pin: bool = True,
        session_name_prefix: str | None = None,
        bootstrap_file: str | None = None,
        on_session_fallback: Optional[Callable[[str], None]] = None,
        plan_tools: list[str] | None = None,
        edit_tools: list[str] | None = None,
        plan_max_turns: int = 25,
        edit_max_turns: int = 25,
    ):
        self.project_dir = str(Path(project_dir).resolve())
        self.session_file = session_file or os.path.join(
            self.project_dir, "logs", "claude_session.txt"
        )
        self.model = model
        self.output_format = output_format
        self.verbose = verbose
        self.auto_pin = auto_pin
        self.bootstrap_file = bootstrap_file
        self.on_session_fallback = on_session_fallback
        self.plan_tools = plan_tools or PLAN_TOOLS
        self.edit_tools = edit_tools or EDIT_TOOLS
        self.plan_max_turns = plan_max_turns
        self.edit_max_turns = edit_max_turns

        # Session naming
        self._session_name_prefix = session_name_prefix
        logs_dir = str(Path(self.session_file).parent)
        self._session_name_file = os.path.join(logs_dir, "claude_session_name.txt")
        self._counter_file = os.path.join(logs_dir, "telegram_session_counter.txt")

        # Session state
        self._session_id: str | None = None
        self._pinned_session_id: str | None = None
        self._session_name: str | None = None
        self._stats = SessionStats()

        self._load_session()
        self._load_session_name()

    # -- Session persistence -----------------------------------------------

    def _load_session(self) -> str | None:
        try:
            sid = Path(self.session_file).read_text().strip()
            if sid:
                self._session_id = sid
                self._pinned_session_id = sid
                return sid
        except FileNotFoundError:
            pass
        return None

    def _save_session(self, sid: str):
        self._session_id = sid
        self._pinned_session_id = sid
        Path(self.session_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.session_file).write_text(sid)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def pinned_session_id(self) -> str | None:
        return self._pinned_session_id

    # -- Session naming ----------------------------------------------------

    def _load_session_name(self) -> str | None:
        try:
            p = Path(self._session_name_file)
            if p.exists():
                name = p.read_text().strip()
                if name:
                    self._session_name = name
                    return name
        except Exception:
            pass
        return None

    def _save_session_name(self, name: str):
        self._session_name = name
        try:
            Path(self._session_name_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self._session_name_file).write_text(name)
        except Exception:
            pass

    def _clear_session_name(self):
        self._session_name = None
        try:
            Path(self._session_name_file).unlink(missing_ok=True)
        except Exception:
            pass

    @property
    def session_name(self) -> str | None:
        return self._session_name

    def next_session_name(self) -> str:
        """Generate next session name like prefix-1, prefix-2, etc."""
        prefix = self._session_name_prefix or "session"
        counter = 0
        try:
            p = Path(self._counter_file)
            if p.exists():
                counter = int(p.read_text().strip())
        except Exception:
            pass
        counter += 1
        try:
            Path(self._counter_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self._counter_file).write_text(str(counter))
        except Exception:
            pass
        return f"{prefix}-{counter}"

    # -- Output parsing ----------------------------------------------------

    @staticmethod
    def _extract_meta(data: dict) -> dict:
        """Extract usage metadata from a CLI result dict."""
        usage = data.get("usage", {})
        model_usage = data.get("modelUsage", {})
        context_window = 0
        for info in model_usage.values():
            context_window = info.get("contextWindow", 0)
        return {
            "duration_ms": data.get("duration_ms", 0),
            "total_cost_usd": data.get("total_cost_usd", 0.0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "context_window": context_window,
        }

    def _parse_json(self, result) -> tuple[str, str, dict]:
        """Parse --output-format json output."""
        try:
            data = json.loads(result.stdout or "{}")
            return data.get("result", ""), data.get("session_id", ""), self._extract_meta(data)
        except json.JSONDecodeError:
            return (result.stdout or "").strip(), "", {}

    def _parse_stream_json(self, result) -> tuple[str, str, dict]:
        """Parse --output-format stream-json output.

        stream-json emits one JSON object per line. We collect all assistant
        text blocks and use the 'result' summary from the final 'result' line.
        If 'result' is empty (e.g. Claude's last turn was a tool call), we
        fall back to the last non-empty assistant text block.
        """
        texts = []
        session_id = ""
        final_result = ""
        meta = {}
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = d.get("type", "")
            if msg_type == "assistant":
                for block in d.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        texts.append(block["text"].strip())
            elif msg_type == "result":
                session_id = d.get("session_id", "")
                final_result = (d.get("result", "") or "").strip()
                meta = self._extract_meta(d)
        # Prefer the result summary; fall back to all assistant text blocks joined
        # (not just texts[-1] — Claude may split a single response across many blocks)
        response = final_result or "\n\n".join(texts)
        return response, session_id, meta

    def _parse(self, result) -> tuple[str, str, dict]:
        if self.output_format == "stream-json":
            return self._parse_stream_json(result)
        return self._parse_json(result)

    # -- Core run ----------------------------------------------------------

    def run(self, prompt: str, allow_edits: bool = False, timeout: int = 240) -> str:
        """Run claude CLI and return response text.

        Args:
            prompt: The prompt to send.
            allow_edits: True = acceptEdits mode (can modify files).
                         False = plan mode (read-only).
            timeout: Subprocess timeout in seconds.

        Returns:
            Claude's response text.
        """
        base_cmd = [
            "claude", "--print",
            "--output-format", self.output_format,
            "--model", self.model,
        ]

        if self.verbose:
            base_cmd.append("--verbose")

        if allow_edits:
            base_cmd.extend(["--permission-mode", "acceptEdits"])
            base_cmd.extend(["--allowedTools"] + self.edit_tools)
            base_cmd.extend(["--max-turns", str(self.edit_max_turns)])
        else:
            base_cmd.extend(["--permission-mode", "plan"])
            base_cmd.extend(["--allowedTools"] + self.plan_tools)
            base_cmd.extend(["--max-turns", str(self.plan_max_turns)])

        env = {**os.environ}
        env.pop("CLAUDECODE", None)  # Allow nested claude invocation

        def _run(cmd):
            # Log the command being executed (truncate prompt for readability)
            cmd_display = []
            for i, arg in enumerate(cmd):
                if i > 0 and cmd[i - 1] == "-p" and len(arg) > 100:
                    cmd_display.append(f"\"{arg[:100]}...\"")
                else:
                    cmd_display.append(arg)
            log.info("Running: %s", " ".join(cmd_display))
            print(f"[claude-cli] Running: {' '.join(cmd_display)}", flush=True)
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=self.project_dir, env=env,
            )

        # ── Try with pinned / current session ──────────────────────────
        resumed = False
        meta = {}
        if self._session_id:
            result = _run(base_cmd + ["--resume", self._session_id, "-p", prompt])
            response_text, returned_session, meta = self._parse(result)

            if result.returncode != 0 and not response_text:
                # Session not found or expired — fall back to fresh
                print(f"[claude-cli] Session {self._session_id[:12]}... expired, starting fresh", flush=True)
                new_name = self.next_session_name() if self._session_name_prefix else None
                if self.on_session_fallback and new_name:
                    self.on_session_fallback(new_name)
                elif self.on_session_fallback:
                    self.on_session_fallback("")
                else:
                    log.warning("Session %s unavailable, starting fresh", self._session_id)

                cmd_suffix = ["-p", prompt]
                if new_name:
                    cmd_suffix = ["--name", new_name] + cmd_suffix
                    self._save_session_name(new_name)
                result = _run(base_cmd + cmd_suffix)
                response_text, returned_session, meta = self._parse(result)
            else:
                resumed = True
        else:
            # No pinned session — generate a name for the new session
            new_name = self.next_session_name() if self._session_name_prefix else None
            print(f"[claude-cli] New session{f' (name={new_name})' if new_name else ''}", flush=True)
            if new_name:
                self._save_session_name(new_name)

            # Check for handoff file to bootstrap context
            actual_prompt = prompt
            if self.bootstrap_file and Path(self.bootstrap_file).exists():
                actual_prompt = (
                    "Run /handoff read now — silently absorb .handoff.md context, "
                    "then answer the user's message below.\n\n" + prompt
                )

            cmd_suffix = ["-p", actual_prompt]
            if new_name:
                cmd_suffix = ["--name", new_name] + cmd_suffix
            result = _run(base_cmd + cmd_suffix)
            response_text, returned_session, meta = self._parse(result)

        # ── Update session state ──────────────────────────────────────
        if returned_session:
            self._session_id = returned_session

            if self.auto_pin:
                # Auto-save on first use and successful resume
                if resumed or not Path(self.session_file).exists():
                    self._save_session(returned_session)
            else:
                # Strict pinning: only save on first-ever or matching resume
                if resumed and returned_session == self._pinned_session_id:
                    self._save_session(returned_session)
                elif not resumed and not self._pinned_session_id:
                    self._save_session(returned_session)

        # ── Accumulate usage stats ────────────────────────────────────
        if meta:
            self._update_stats(meta)

        if result.returncode != 0 and not response_text:
            error_msg = (result.stderr or "unknown").strip()[:500]
            print(f"[claude-cli] Failed (exit={result.returncode}): {error_msg[:200]}", flush=True)
            response_text = f"Error: {error_msg}"
        else:
            print(f"[claude-cli] Finished (exit={result.returncode}, {len(response_text)} chars response)", flush=True)

        return response_text

    # -- Stats -------------------------------------------------------------

    def _update_stats(self, meta: dict):
        """Accumulate usage stats from a single CLI invocation."""
        s = self._stats
        s.total_turns += 1
        s.total_duration_ms += meta.get("duration_ms", 0)
        s.total_cost_usd += meta.get("total_cost_usd", 0.0)
        s.total_input_tokens += meta.get("input_tokens", 0)
        s.total_output_tokens += meta.get("output_tokens", 0)
        s.total_cache_read_tokens += meta.get("cache_read_input_tokens", 0)
        s.total_cache_creation_tokens += meta.get("cache_creation_input_tokens", 0)
        if meta.get("context_window"):
            s.context_window = meta["context_window"]
        s.last_input_tokens = meta.get("input_tokens", 0) + meta.get("cache_read_input_tokens", 0)

    @property
    def stats(self) -> SessionStats:
        return self._stats

    @property
    def context_pct(self) -> float | None:
        """Percentage of context window consumed (0-100), or None if unknown."""
        s = self._stats
        if s.context_window <= 0 or s.last_input_tokens <= 0:
            return None
        return min(100.0, (s.last_input_tokens / s.context_window) * 100)

    def set_model(self, model: str) -> bool:
        """Switch model for subsequent CLI invocations."""
        if model in VALID_MODELS:
            self.model = model
            return True
        return False

    # -- Session management -----------------------------------------------

    def flush(self, flush_prompt: str | None = None, timeout: int = 240):
        """Run a flush prompt (e.g. handoff write), then clear the session.

        Args:
            flush_prompt: Custom prompt to run before clearing. If None, skips.
            timeout: Subprocess timeout.
        """
        if not self._session_id:
            return

        if flush_prompt:
            try:
                self.run(flush_prompt, allow_edits=True, timeout=timeout)
            except Exception:
                log.exception("Failed to flush session")

        self._session_id = None
        self._pinned_session_id = None
        self._clear_session_name()
        self._stats = SessionStats()
        try:
            Path(self.session_file).unlink()
        except FileNotFoundError:
            pass

    def clear(self):
        """Clear session without flushing."""
        self._session_id = None
        self._pinned_session_id = None
        self._clear_session_name()
        self._stats = SessionStats()
        try:
            Path(self.session_file).unlink()
        except FileNotFoundError:
            pass

    def pin(self, session_id: str):
        """Explicitly pin a session ID."""
        self._save_session(session_id)
