# TeleClaude - Development Guide

## What is this?
A pip package providing a Telegram bot framework with Claude Code session integration. Bots built on teleclaude can send prompts to Claude Code, receive AI-assisted code analysis, and apply edits via a plan/approve workflow.

## Local development
```bash
pip install -e .              # install in editable mode
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
- `teleclaude/session_cli.py` - Claude Code CLI subprocess wrapper (proven path)
- `teleclaude/session_channel.py` - Channel API stub (future, same interface)
- `teleclaude/base_bot.py` - Telegram base class with plan/approve/reject workflow
- `teleclaude/self_update.py` - PID file + os.execv restart

## Conventions
- Shared logic changes go here, not patched in individual bot repos
- Both session approaches must expose the same interface (`run`, `flush`, `clear`, `pin`)
- Keep `base_bot.py` extensible via hooks (`on_message`, `on_callback`, `extra_commands`)

## Self-update flow
1. Claude edits source files via `/approve`
2. User sends `/restart` in Telegram
3. Bot process replaces itself via `os.execv` - new code is loaded
