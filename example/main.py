"""Minimal TeleClaude bot  - 30 lines to a working Telegram + Claude Code bot."""

import os
from dotenv import load_dotenv
from teleclaude import ClaudeSession, TeleClaudeBot, kill_previous

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(PROJECT_DIR, "bot.pid")


class MyBot(TeleClaudeBot):
    """Example bot  - override on_message to add your own logic."""

    async def on_message(self, update, text):
        # Default: everything goes to Claude.
        # Override this to add domain-specific routing.
        await self._route_to_claude(update, text)


def main():
    kill_previous(PIDFILE)
    session = ClaudeSession(project_dir=PROJECT_DIR)
    bot = MyBot(token=TOKEN, chat_id=CHAT_ID, claude_session=session, bot_name="MyBot")
    bot.run()


if __name__ == "__main__":
    main()
