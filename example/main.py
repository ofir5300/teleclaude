"""Example TeleClaude bot showing how to add custom behavior.

Zero-config version (no subclass needed):
    session = ClaudeSession(project_dir=".")
    bot = TeleClaudeBot(token=TOKEN, chat_id=CHAT_ID, claude_session=session)
    bot.run()

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
    """A bot that adds /status and /improve on top of the built-in commands."""

    def extra_commands(self):
        return [
            ("status", "Show git status", self.cmd_status),
            ("improve", "Ask Claude to improve this bot", self.cmd_improve),
        ]

    async def cmd_status(self, update, context):
        """Show the current git status of the project."""
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        await update.message.reply_text(result.stdout or "No git history.")

    async def cmd_improve(self, update, context):
        """Use the plan/approve flow to let Claude improve this bot."""
        # Step 1: Ask Claude for a plan (read-only)
        plan = await self._run_claude_async(
            "Look at main.py and suggest one small improvement. "
            "Be specific about what to change and why.",
            allow_edits=False,
        )
        # Step 2: Show the plan and wait for /approve or /reject
        await self.send_plan_for_approval(
            update,
            plan_text=plan,
            execute_prompt="Implement the improvement you just suggested.",
        )

    async def on_message(self, update, text):
        """Route messages: 'improve' triggers the plan flow, rest goes to Claude."""
        if "improve" in text.lower():
            await self.cmd_improve(update, None)
        else:
            # Default: send to Claude in read-only mode
            await self._route_to_claude(update, text)


def main():
    kill_previous(PIDFILE)
    session = ClaudeSession(project_dir=PROJECT_DIR)
    bot = MyBot(
        token=TOKEN,
        chat_id=CHAT_ID,
        claude_session=session,
        bot_name="MyBot",
    )
    bot.run()


if __name__ == "__main__":
    main()
