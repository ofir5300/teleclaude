"""Telegram bot base class with Claude Code plan/approve/reject workflow.

Subclass TeleClaudeBot and override on_message() to add domain-specific logic.
"""

import asyncio
import logging
import os
from typing import Callable

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from teleclaude.session_cli import ClaudeSession
from teleclaude.self_update import restart

log = logging.getLogger(__name__)

# Telegram message length limit
_TG_MAX_LEN = 4000


class TeleClaudeBot:
    """Base Telegram bot with built-in Claude Code integration.

    Built-in commands:
        /approve         - Execute Claude's pending plan
        /reject          - Discard pending plan
        /restart         - Restart the bot process
        /help            - Show commands

    Free-text messages are automatically routed to Claude in read-only mode.

    Subclass and override:
        on_message(update, text)   - Handle free-text messages (default: route to Claude)
        on_callback(update, data)  - Handle custom inline keyboard callbacks
        extra_commands()           - Return list of (name, description, handler) tuples
        help_text()                - Return custom help string
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        claude_session: ClaudeSession,
        bot_name: str = "TeleClaudeBot",
    ):
        self.token = token
        self.chat_id = chat_id
        self.claude = claude_session
        self.bot_name = bot_name

        # Claude plan/approve state
        self._claude_busy = False
        self._claude_pending_prompt: str | None = None

    # -- Hooks for subclasses ---------------------------------------------

    async def on_message(self, update: Update, text: str):
        """Override to handle free-text messages that aren't URLs or commands.

        Default: routes everything to Claude in read-only mode.
        """
        await self._route_to_claude(update, text)

    async def on_callback(self, update: Update, data: str) -> bool:
        """Override to handle custom inline keyboard callbacks.

        Return True if handled, False to fall through to default handling.
        """
        return False

    def extra_commands(self) -> list[tuple[str, str, Callable]]:
        """Override to register additional commands.

        Returns list of (command_name, description, async_handler) tuples.
        Handler signature: async def handler(update, context)
        """
        return []

    def help_text(self) -> str:
        """Override to customize help message."""
        lines = [
            f"*{self.bot_name} Commands*\n",
            "Send any message to chat with Claude Code",
            "/approve - Execute Claude's pending plan",
            "/reject - Discard pending plan",
            "/restart - Restart the bot",
            "/help - This message",
        ]
        for name, desc, _ in self.extra_commands():
            lines.append(f"/{name} - {desc}")
        return "\n".join(lines)

    # -- Built-in command handlers -----------------------------------------

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(self.help_text(), parse_mode="Markdown")

    async def _cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._claude_pending_prompt:
            await update.message.reply_text("No pending plan to approve.")
            return
        if self._claude_busy:
            await update.message.reply_text("Claude is already working. Please wait.")
            return

        self._claude_busy = True
        await update.message.reply_text("Approved! Claude is implementing...")

        try:
            response = await asyncio.to_thread(
                self.claude.run, self._claude_pending_prompt, allow_edits=True
            )
            display = self._truncate(response)
            await update.message.reply_text(
                f"*Done!*\n\n{display}", parse_mode="Markdown"
            )
            self._claude_pending_prompt = None
        except Exception as e:
            log.exception("Claude edit failed")
            await update.message.reply_text(f"Error: {e}")
        finally:
            self._claude_busy = False

    async def _cmd_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._claude_pending_prompt:
            await update.message.reply_text("No pending plan to reject.")
            return
        self._claude_pending_prompt = None
        await update.message.reply_text("Plan rejected.")

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Restarting...")
        restart()

    # -- Claude routing ----------------------------------------------------

    async def _route_to_claude(self, update: Update, text: str):
        """Send text to Claude in read-only mode and reply with the response."""
        if self._claude_busy:
            await update.message.reply_text("Claude is already working. Please wait.")
            return

        self._claude_busy = True
        await update.message.reply_text("Thinking...")

        try:
            response = await asyncio.to_thread(
                self.claude.run, text, allow_edits=False
            )
            display = self._truncate(response)
            await update.message.reply_text(display or "No response from Claude.")
        except Exception as e:
            log.exception("Claude request failed")
            await update.message.reply_text(f"Error: {e}")
        finally:
            self._claude_busy = False

    async def send_plan_for_approval(self, update: Update, plan_text: str, execute_prompt: str):
        """Show a plan to the user and store the execute prompt for /approve.

        Call this from on_message() when you want the plan/approve flow.
        """
        self._claude_pending_prompt = execute_prompt
        display = self._truncate(plan_text)
        await update.message.reply_text(
            f"*Claude's Plan:*\n\n{display}\n\n"
            f"Use /approve to execute or /reject to cancel.",
            parse_mode="Markdown",
        )

    # -- Callback query handler --------------------------------------------

    async def _callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        handled = await self.on_callback(update, query.data)
        if not handled:
            await query.edit_message_text("Unknown action.")

    # -- Free-text handler -------------------------------------------------

    async def _free_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text or ""
        await self.on_message(update, text)

    # -- Build & run -------------------------------------------------------

    def build(self) -> Application:
        """Build the telegram Application with all handlers registered."""
        app = Application.builder().token(self.token).build()

        # Built-in commands
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("start", self._cmd_help))
        app.add_handler(CommandHandler("approve", self._cmd_approve))
        app.add_handler(CommandHandler("reject", self._cmd_reject))
        app.add_handler(CommandHandler("restart", self._cmd_restart))

        # Extra commands from subclass
        commands = [
            BotCommand("help", "Show commands"),
            BotCommand("approve", "Approve Claude's plan"),
            BotCommand("reject", "Reject Claude's plan"),
            BotCommand("restart", "Restart the bot"),
        ]
        for name, desc, handler in self.extra_commands():
            app.add_handler(CommandHandler(name, handler))
            commands.append(BotCommand(name, desc))

        # Callbacks and free text
        app.add_handler(CallbackQueryHandler(self._callback_handler))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._free_text_handler)
        )

        # Set commands on startup
        chat_id = self.chat_id
        bot_name = self.bot_name

        async def post_init(application: Application):
            await application.bot.set_my_commands(commands)
            log.info("%s started", bot_name)
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"*{bot_name} started*",
                parse_mode="Markdown",
            )

        app.post_init = post_init
        return app

    def run(self):
        """Build and run the bot (blocking)."""
        app = self.build()
        app.run_polling(drop_pending_updates=True)

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _truncate(text: str, limit: int = _TG_MAX_LEN) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 20] + "\n\n... (truncated)"
