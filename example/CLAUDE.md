# Bot Project - Claude Code Instructions

## What is this?
A Telegram bot powered by [TeleClaude](https://github.com/ofir5300/teleclaude). Claude Code sessions run as subprocesses with plan/edit permission modes.

## Running locally
```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
pip install teleclaude  # or: pip install -e /path/to/teleclaude for local dev
python main.py
```

## How Claude Code integration works
- Free-text messages in Telegram are routed to Claude Code in **plan mode** (read-only)
- `/approve` switches to **edit mode** - Claude can now modify files
- `/reject` discards the pending plan
- `/restart` reloads the bot process to pick up code changes

## Architecture
- `main.py` - Entry point, subclass of `TeleClaudeBot`
- `teleclaude.ClaudeSession` - Claude CLI subprocess wrapper (session persistence, plan/edit modes)
- `teleclaude.TeleClaudeBot` - Telegram base class with /approve, /reject, /restart + free-text routing
- `teleclaude.self_update` - PID file management + `os.execv` restart

## Session management
- Session ID persisted in `logs/claude_session.txt`
- `--resume` flag provides multi-turn context continuity
- `flush()` creates a summary in `.claude/last_session.md` before starting fresh
