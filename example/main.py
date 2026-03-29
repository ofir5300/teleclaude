"""Example TeleClaude bot showing how to add custom behavior.

Zero-config version (no subclass needed):
    session = ClaudeSession(project_dir=".")
    bot = TeleClaudeBot(token=TOKEN, chat_id=CHAT_ID, claude_session=session)
    bot.start_polling()

This example shows the subclass approach - adding custom commands and
message routing on top of the built-in Claude Code integration.
"""

import os
import subprocess
from dotenv import load_dotenv
from teleclaude import ClaudeSession, TeleClaudeBot, kill_previous

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(PROJECT_DIR, "bot.pid")


class MyBot(TeleClaudeBot):
    """A bot that adds /status on top of the built-in commands."""

    def domain_commands(self):
        return {
            "/status": (self.cmd_status, "Show git status"),
        }

    def cmd_status(self):
        """Show the current git log of the project."""
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, cwd=self._project_dir,
        )
        self.send(f"<pre>{result.stdout or 'No git history.'}</pre>")


def main():
    kill_previous(PIDFILE)
    session = ClaudeSession(project_dir=PROJECT_DIR)
    bot = MyBot(
        token=TOKEN,
        chat_id=CHAT_ID,
        claude_session=session,
        project_dir=PROJECT_DIR,
    )
    bot.start_polling()

    # Keep main thread alive
    import time
    try:
        while bot.running:
            time.sleep(1)
    except KeyboardInterrupt:
        bot.stop_polling()


if __name__ == "__main__":
    main()
