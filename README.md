
<p align="center">
  <img src="assets/telegram.svg" alt="Telegram" height="80">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/claude.svg" alt="Claude Code" height="80">
</p>

# TeleClaude

**A Python framework for building Telegram bots that use [Claude Code](https://github.com/anthropics/claude-code) to read, plan, and edit their own codebase - with human-in-the-loop approval, voice transcription via OpenAI Whisper, and self-restarting deployment.**

<sub>`anthropic` `claude-code` `claude-code-channels` `telegram-bot` `ai-agent` `self-improving-software` `whisper` `voice-to-code` `agentic-coding` `python`</sub>

<p align="center">
  <a href="https://pypi.org/project/teleclaude/"><img src="https://img.shields.io/pypi/v/teleclaude?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/teleclaude/"><img src="https://img.shields.io/pypi/pyversions/teleclaude" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/ofir5300/teleclaude" alt="License"></a>
</p>

---

## The idea: let Claude Code edit itself through Telegram

Your bot is a thin Telegram relay. **[Claude Code](https://github.com/anthropics/claude-code) is the agentic part** - it reads the codebase, proposes changes, and writes files when you approve. The bot just forwards your messages to Claude Code and sends back the response.

The result is a **closed-loop development cycle**: `chat -> analyze -> plan -> approve -> edit -> restart`. No IDE, no SSH, no deploy pipeline. Just a conversation with your running application from your phone - or even a voice message transcribed via [Whisper](https://github.com/openai/whisper).

**What about [Claude Code Channels](https://docs.anthropic.com/en/docs/claude-code/channels)?** Anthropic's official Telegram/Discord/iMessage channels are a promising direction, but they're still in research preview - when we last tested them, the experience wasn't stable enough for production use. TeleClaude was built to fill that gap: your bot runs Claude Code as a subprocess on the server where it's deployed, with a plan/approve workflow and self-restart baked in. No local machine needed, no `--channels` flag, no Bun dependency - just `pip install` and go. If Channels matures into a solid production path, we'd love to add it as an alternative backend - [contributions welcome](https://github.com/ofir5300/teleclaude/issues).

<img src="assets/claude-bot.png" alt="Claude Bot" width="60">

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

<a href="assets/example-from-polybot.png"><img align="right" src="assets/example-from-polybot.png" alt="Claude control example from PolyBot" height="190"></a>

1. You send a message in Telegram
2. TeleClaude routes it to Claude Code in **read-only plan mode**
3. Claude analyzes your codebase and proposes a plan
4. You `/approve` or `/reject` from Telegram
5. On approve, Claude executes with file-edit permissions
6. `/restart` reloads the bot to pick up code changes
7. The application is now running its improved version of itself

<br clear="right">

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
import os, time
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
bot.start_polling()

try:
    while bot.running:
        time.sleep(1)
except KeyboardInterrupt:
    bot.stop_polling()
```

That's it. Send any message in Telegram and Claude Code responds. Built-in commands:

| Command     | What it does                                                        |
| ----------- | ------------------------------------------------------------------- |
| Free text   | Chat with Claude Code (read-only mode)                              |
| Voice msg   | Transcribed via Whisper, then routed to Claude                      |
| `/claude`   | Interactive menu: model switcher, session info, flush, approve/reject |
| `/approve`  | Execute Claude's pending plan (allows file edits)                   |
| `/reject`   | Discard the pending plan                                            |
| `/session`  | Session management (`/session pin <id>`, `/session clear`)          |
| `/context`  | Check Claude Code availability (rate-limit detection)               |
| `/restart`  | Restart the bot process (picks up code changes)                     |
| `/help`     | Show all available commands                                         |

### Add your own commands

Subclass `TeleClaudeBot` and override `domain_commands()` to register custom `/commands`:

```python
import subprocess
from teleclaude import ClaudeSession, TeleClaudeBot, kill_previous

class MyBot(TeleClaudeBot):
    def domain_commands(self):
        return {
            "/status": (self.cmd_status, "Show git status"),
        }

    def cmd_status(self):
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, cwd=self._project_dir,
        )
        self.send(f"<pre>{result.stdout or 'No git history.'}</pre>")
```

Other hooks you can override:

| Hook | Purpose |
| ---- | ------- |
| `on_domain_callback(data, message_id)` | Handle inline keyboard callbacks |
| `help_text()` | Customize `/help` output |
| `on_restart()` | Customize restart behavior |
| `plan_prompt_wrapper(text)` | Customize the prompt sent to Claude in plan mode |

## Features

### Voice messages

Send a voice message in Telegram and it gets auto-transcribed via [OpenAI Whisper](https://github.com/openai/whisper), then routed to Claude. Install the optional dependency:

```bash
pip install openai-whisper
```

The Whisper model (`base`) is lazy-loaded on first voice message. If not installed, the bot replies with install instructions.

### Session handoff

The `/claude` menu includes **Flush & New Session**, which:
1. Asks Claude to write a `.handoff.md` summary of the current session context
2. Clears the session pin
3. On the next message, a new session bootstraps from `.handoff.md` automatically

To enable automatic handoff bootstrap, pass `bootstrap_file` to `ClaudeSession`:

```python
session = ClaudeSession(project_dir=".", bootstrap_file=".handoff.md")
```

### Rate-limit detection

When Claude returns a rate-limit error, the bot automatically starts background polling (every 5 min, up to 12 h) and notifies you when Claude is back online. You can also manually check via `/context` or the `/claude` menu.

## Configuration reference

### `ClaudeSession` options

```python
session = ClaudeSession(
    project_dir=".",              # repo root (resolved to absolute path)
    model="opus",                 # "opus", "sonnet", or "haiku"
    output_format="json",         # "json" or "stream-json"
    bootstrap_file=".handoff.md", # auto-inject on new sessions (None to disable)
    auto_pin=True,                # auto-save session ID on first use
    session_name_prefix="mybot",  # generates mybot-1, mybot-2, etc.
    plan_max_turns=25,            # max turns in plan (read-only) mode
    edit_max_turns=25,            # max turns in edit mode
    plan_tools=["Read", "Grep"], # override default plan-mode tools
    edit_tools=["Read", "Edit"], # override default edit-mode tools
    on_session_fallback=callback, # called when pinned session expires
)
```

## Development

```bash
git clone https://github.com/ofir5300/teleclaude.git
cd teleclaude
pip install -e .
cd example && cp .env.example .env  # fill in your tokens
python main.py
```

## Contributing

Contributions welcome! Open an issue or PR on [GitHub](https://github.com/ofir5300/teleclaude).

## License

[MIT](LICENSE) - Ofir Cohen
