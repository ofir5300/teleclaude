
<p align="center">
  <img src="assets/telegram.svg" alt="Telegram" height="60">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/claude.svg" alt="Claude Code" height="60">
</p>

# TeleClaude

**Build self-improving applications with Telegram + Claude Code.**

<p align="center">
  <a href="https://pypi.org/project/teleclaude/"><img src="https://img.shields.io/pypi/v/teleclaude?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/teleclaude/"><img src="https://img.shields.io/pypi/pyversions/teleclaude" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/ofir5300/teleclaude" alt="License"></a>
</p>

---

## The idea: conversational self-improvement

TeleClaude enables **agentic self-development** - your deployed application becomes its own development environment. You talk to it through Telegram, it understands its own codebase via [Claude Code](https://github.com/anthropics/claude-code), proposes changes, and - once you approve - rewrites its own source code and restarts itself with the improvements live.

This is a **closed-loop development cycle**: `chat -> analyze -> plan -> approve -> edit -> restart`. No IDE, no SSH, no deploy pipeline. Just a conversation with your running application from your phone.

The technical pattern is sometimes called **reflexive software** or **agentic bootstrapping** - a system that can inspect and modify itself through an AI agent. TeleClaude packages this into a simple Python framework.

**Why?** [Claude Code](https://docs.anthropic.com/en/docs/claude-code) is a powerful agentic coding tool (82K+ stars), but it lives in your terminal. TeleClaude lets you talk to it from anywhere - your phone, a group chat, on the go - while keeping the human-in-the-loop approval flow that makes it safe for real codebases.

## How it works

```
+----------------+        +-----------------+        +--------------------+
|   Telegram     |  msg   |   TeleClaude    | stdin  |    Claude Code     |
|   (phone)      |------->|   (Python bot)  |------->|    (subprocess)    |
|                |<-------|                 |<-------|                    |
|                | reply  |  plan/approve   | stdout |   reads/edits      |
|                |        |  workflow       |        |   your codebase    |
+----------------+        +-----------------+        +--------------------+
```

1. You send a message in Telegram
2. TeleClaude routes it to Claude Code in **read-only plan mode**
3. Claude analyzes your codebase and proposes a plan
4. You `/approve` or `/reject` from Telegram
5. On approve, Claude executes with file-edit permissions
6. `/restart` reloads the bot to pick up code changes
7. The application is now running its improved version of itself

## Quickstart

### Prerequisites

- Python >= 3.10
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Install

```bash
pip install teleclaude
```

### Create your bot

```python
import os
from dotenv import load_dotenv
from teleclaude import ClaudeSession, TeleClaudeBot, kill_previous

load_dotenv()

kill_previous("bot.pid")
session = ClaudeSession(project_dir=".")
bot = TeleClaudeBot(
    token=os.environ["TELEGRAM_BOT_TOKEN"],
    chat_id=os.environ["TELEGRAM_CHAT_ID"],
    claude_session=session,
)
bot.run()
```

That's it. Send any message in Telegram and Claude Code responds. Built-in commands:

| Command     | What it does                                      |
| ----------- | ------------------------------------------------- |
| Free text   | Chat with Claude Code (read-only mode)            |
| `/approve`  | Execute Claude's pending plan (allows file edits) |
| `/reject`   | Discard the pending plan                          |
| `/restart`  | Restart the bot process (picks up code changes)   |

### Add your own commands

```python
class MyBot(TeleClaudeBot):
    def extra_commands(self):
        return [("improve", "Ask Claude to improve this bot", self.cmd_improve)]

    async def cmd_improve(self, update, context):
        # Ask Claude for a plan (read-only), then show it for approval
        plan = await self._run_claude_async("Suggest one improvement to main.py.")
        await self.send_plan_for_approval(update, plan, "Implement the improvement.")

    async def on_message(self, update, text):
        if "deploy" in text:
            await update.message.reply_text("Deploying...")
        else:
            await self._route_to_claude(update, text)
```

## Session modes

### CLI mode (`ClaudeSession`) - recommended

The battle-tested approach. Spawns `claude --print` as a subprocess per message with JSON output parsing. Features:

- **Session persistence**: IDs saved to disk, resumed with `--resume` for multi-turn context
- **Two permission modes**: plan (read-only) and edit (file modifications)
- **Automatic fallback**: expired sessions gracefully restart fresh
- **Configurable**: custom model, tools, max turns

### Channel mode (`ClaudeChannelSession`) - coming soon

Stub for the upcoming Anthropic SDK channel API. Same interface as CLI mode - swap with a config flag when available.

## Architecture

```
teleclaude/
  session_cli.py      # Claude Code subprocess wrapper with session persistence
  session_channel.py  # Channel API stub (same interface, future)
  base_bot.py         # Telegram base class: /approve, /reject, /restart + free-text routing
  self_update.py      # PID file management + os.execv restart
```

| Module            | What it does                                                               |
| ----------------- | -------------------------------------------------------------------------- |
| `ClaudeSession`   | Wraps `claude --print` with session pinning, plan/edit modes, JSON parsing |
| `TeleClaudeBot`   | Async Telegram bot with plan/approve/execute workflow. Extensible via hooks |
| `kill_previous()` | PID file management - kills stale bot processes on startup                 |
| `restart()`       | `os.execv` process replacement - reload code without downtime              |

## Local development

```bash
git clone https://github.com/ofir5300/teleclaude.git
cd teleclaude
pip install -e .
cd example
cp .env.example .env  # fill in your tokens
python main.py
```

## Related resources

**Claude Code:**

- [Claude Code - Official GitHub](https://github.com/anthropics/claude-code) (82K+ stars)
- [Claude Code Documentation](https://docs.anthropic.com/en/docs/claude-code)
- [Claude Code Overview](https://code.claude.com/docs/en/overview)

**Guides and tutorials:**

- [Claude Code Tutorial for Beginners 2026](https://dev.to/ayyazzafar/claude-code-tutorial-for-beginners-2026-from-installation-to-building-your-first-project-1lma) - dev.to
- [Claude Code: A Guide With Practical Examples](https://www.datacamp.com/tutorial/claude-code) - DataCamp
- [Claude Code CLI Cheatsheet](https://shipyard.build/blog/claude-code-cheat-sheet/) - Shipyard

**Community:**

- [What makes Claude Code so good](https://news.ycombinator.com/item?id=44998295) - Hacker News discussion
- [awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) - Curated plugins, hooks, and commands

## Contributing

Contributions welcome! Open an issue or PR on [GitHub](https://github.com/ofir5300/teleclaude).

## License

[MIT](LICENSE) - Ofir Cohen
