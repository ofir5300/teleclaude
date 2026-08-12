# TeleClaude - Development Guide

## What is this?
A pip package providing a Telegram bot framework with Claude Code session integration. Bots built on teleclaude can send prompts to Claude Code, receive AI-assisted code analysis, and apply edits via a plan/approve workflow.

## Local development
```bash
pip install -e ".[dev]"       # install with ruff + pytest
pytest                        # run the test suite (no network, no claude CLI)
ruff check .                  # lint
cd example && python main.py  # test with the example bot
```

## Testing changes against a consuming bot
From the bot repo that depends on teleclaude:
```bash
pip install -e /path/to/teleclaude
python main.py
```
Since it's an editable install, code changes in teleclaude are picked up immediately (restart the bot to reload).

## Package structure
`TeleClaudeBot` is assembled from mixins - one concern per file, all sharing the bot's state:

- `teleclaude/base_bot.py` - the class itself: state, command registry, extensibility hooks
- `teleclaude/_telegram.py` - Telegram HTTP API (send/edit/get_updates) + retry helper
- `teleclaude/_polling.py` - long-poll loop, update dedup, dispatch
- `teleclaude/_claude_runner.py` - plan/approve/reject lifecycle for free text
- `teleclaude/_claude_menu.py` - `/claude` inline-keyboard UI
- `teleclaude/_availability.py` - `/context`, availability polling, usage-limit watcher
- `teleclaude/_voice.py` - voice -> Whisper -> Claude
- `teleclaude/session_cli.py` - Claude Code CLI subprocess wrapper (proven path)
- `teleclaude/session_channel.py` - Channel API stub (future, same interface)
- `teleclaude/self_update.py` - PID file + os.execv restart

## Conventions
- Shared logic changes go here, not patched in individual bot repos
- Both session approaches must expose the same interface (`run`, `flush`, `clear`, `pin`)
- Keep `base_bot.py` extensible via hooks (`domain_commands`, `on_domain_callback`, `help_text`, `on_restart`, `plan_prompt_wrapper`)
- Every Claude availability probe goes through `AvailabilityMixin._run_probe` / `_probe_status` - don't inline another `subprocess.run(["claude", ...])`
- Long-running background work is a daemon thread; anything sleeping for minutes waits on an `Event` so it can be interrupted
- Bot-visible logging is `print(..., flush=True)` with a `[tag]` prefix, so launchd/journald captures it unbuffered

## Versioning and release
Version comes from git tags via setuptools-scm - there is no version string in the source. Tag `vX.Y.Z` and push; `.github/workflows/publish.yml` builds, publishes to PyPI, and cuts a GitHub Release.

## Self-update flow
1. Claude edits source files via `/approve`
2. User sends `/restart` in Telegram
3. Bot process replaces itself via `os.execv` - new code is loaded
