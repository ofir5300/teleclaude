"""Claude Code subprocess wrapper with session persistence.

Two modes: plan (read-only analysis) and edit (file modifications).
Session IDs are persisted to disk and resumed with --resume for multi-turn context.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Default tools for each mode - callers can override
PLAN_TOOLS = ["Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "Agent"]
EDIT_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Agent"]


class ClaudeSession:
    """Manages a persistent Claude Code CLI session with plan/edit modes.

    Usage:
        session = ClaudeSession(project_dir="/path/to/repo")
        plan = session.run("Analyze this code", allow_edits=False)
        result = session.run("Implement the plan", allow_edits=True)
    """

    def __init__(
        self,
        project_dir: str,
        session_file: str | None = None,
        last_session_file: str | None = None,
        model: str = "opus",
        plan_tools: list[str] | None = None,
        edit_tools: list[str] | None = None,
        plan_max_turns: int = 10,
        edit_max_turns: int = 15,
    ):
        self.project_dir = str(Path(project_dir).resolve())
        self.session_file = session_file or os.path.join(
            self.project_dir, "logs", "claude_session.txt"
        )
        self.last_session_file = last_session_file or os.path.join(
            self.project_dir, ".claude", "last_session.md"
        )
        self.model = model
        self.plan_tools = plan_tools or PLAN_TOOLS
        self.edit_tools = edit_tools or EDIT_TOOLS
        self.plan_max_turns = plan_max_turns
        self.edit_max_turns = edit_max_turns

        self._session_id: str | None = None
        self._load_session()

    # -- Session persistence -----------------------------------------------

    def _load_session(self) -> str | None:
        if self._session_id:
            return self._session_id
        try:
            sid = Path(self.session_file).read_text().strip()
            if sid:
                self._session_id = sid
                return sid
        except FileNotFoundError:
            pass
        return None

    def _save_session(self, sid: str):
        self._session_id = sid
        Path(self.session_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.session_file).write_text(sid)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # -- Core run ------------------------------------------------------

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
            "claude", "--print", "--output-format", "json",
            "--model", self.model,
        ]

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
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=self.project_dir, env=env,
            )

        def _parse(result):
            try:
                data = json.loads(result.stdout or "{}")
                return data.get("result", ""), data.get("session_id", "")
            except json.JSONDecodeError:
                return (result.stdout or "").strip(), ""

        # Try with existing session for context continuity
        resumed = False
        if self._session_id:
            result = _run(base_cmd + ["--resume", self._session_id, "-p", prompt])
            response_text, returned_session = _parse(result)

            if result.returncode != 0 and not response_text:
                log.warning("Session %s unavailable, starting fresh", self._session_id)
                result = _run(base_cmd + ["-p", prompt])
                response_text, returned_session = _parse(result)
            else:
                resumed = True
        else:
            result = _run(base_cmd + ["-p", prompt])
            response_text, returned_session = _parse(result)

        # Persist session ID
        if returned_session:
            self._session_id = returned_session
            # ASSUMPTION: auto-save session on first use and successful resume.
            # For stricter pinning (never auto-overwrite), override _save_session.
            if resumed or not Path(self.session_file).exists():
                self._save_session(returned_session)

        if result.returncode != 0 and not response_text:
            error_msg = (result.stderr or "unknown").strip()[:500]
            response_text = f"Error: {error_msg}"

        return response_text

    # -- Session management -----------------------------------------------

    def flush(self, timeout: int = 120):
        """Ask Claude to summarize context to last_session.md, then start fresh."""
        if not self._session_id:
            return

        summary_prompt = (
            f"Summarize the key context, decisions, and state from this conversation "
            f"into {self.last_session_file}. Include: what was done, what's pending, "
            f"and any important context for the next session."
        )
        try:
            self.run(summary_prompt, allow_edits=True, timeout=timeout)
        except Exception:
            log.exception("Failed to flush session")

        self._session_id = None
        try:
            Path(self.session_file).unlink()
        except FileNotFoundError:
            pass

    def clear(self):
        """Clear session without flushing."""
        self._session_id = None
        try:
            Path(self.session_file).unlink()
        except FileNotFoundError:
            pass

    def pin(self, session_id: str):
        """Explicitly pin a session ID."""
        self._save_session(session_id)
