"""Claude Code session via Anthropic channel API (programmatic two-way communication).

Same interface as session_cli.ClaudeSession so bots can swap with a config flag.

TODO: The Anthropic SDK channel feature for programmatic Claude Code communication
      is not yet publicly documented. Once available, replace the stub below.
      Track: https://docs.anthropic.com/en/docs/claude-code
"""

import logging

log = logging.getLogger(__name__)


class ClaudeChannelSession:
    """Stub - same interface as ClaudeSession for future channel API support.

    Currently raises NotImplementedError. When the Anthropic SDK exposes
    a channel/session API, this class will provide:
   - Persistent two-way communication without subprocess spawning per message
   - Lower latency than CLI mode
   - Native streaming support
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
        self.project_dir = project_dir
        self.model = model
        # ASSUMPTION: channel API will support the same tool allowlists
        self.plan_tools = plan_tools
        self.edit_tools = edit_tools
        self._session_id: str | None = None
        log.warning(
            "ClaudeChannelSession is a stub - channel API not yet available. "
            "Use ClaudeSession (CLI mode) instead."
        )

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def run(self, prompt: str, allow_edits: bool = False, timeout: int = 240) -> str:
        """Run a prompt via the channel API.

        TODO: Implement once Anthropic SDK exposes channel/session support.
        For now, raises NotImplementedError.
        """
        raise NotImplementedError(
            "Channel API not yet available. Use ClaudeSession (CLI mode) instead."
        )

    def flush(self, timeout: int = 120):
        raise NotImplementedError("Channel API not yet available.")

    def clear(self):
        self._session_id = None

    def pin(self, session_id: str):
        self._session_id = session_id
