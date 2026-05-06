"""Polling loop and update dispatch."""

import threading
import time


class PollingMixin:
    """Long-poll loop and update routing. Expects command/callback handlers from sibling mixins."""

    def start_polling(self):
        """Start listening for commands in a background daemon thread."""
        self.running = True
        self._register_commands()

        def poll_loop():
            print("[i] Telegram command listener started", flush=True)
            while self.running:
                try:
                    updates = self.get_updates(timeout=30)
                    if updates:
                        print(f"[i] Telegram: {len(updates)} update(s) received", flush=True)
                    for update in updates:
                        try:
                            self.process_update(update)
                        except Exception as e:
                            print(f"[!] Telegram command error: {e}", flush=True)
                            try:
                                self.send(f"❌ Command error: {e}")
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[!] Telegram polling error: {e}", flush=True)
                    time.sleep(5)

        thread = threading.Thread(target=poll_loop, daemon=True)
        thread.start()
        return thread

    def stop_polling(self):
        """Stop listening for commands."""
        self.running = False

    def process_update(self, update: dict):
        """Process a single update (message or callback_query)."""
        uid = update.get("update_id")
        if uid is not None:
            if uid in self._seen_update_ids:
                return  # Telegram re-delivery — skip
            self._seen_update_ids.add(uid)
            if len(self._seen_update_ids) > 200:
                self._seen_update_ids.discard(min(self._seen_update_ids))
            self.last_update_id = uid

        callback = update.get("callback_query")
        if callback:
            cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            if cb_chat_id == self.chat_id:
                self._handle_callback(callback)
            return

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != self.chat_id:
            return

        voice = message.get("voice") or message.get("audio")
        if voice:
            self._handle_voice_message(voice.get("file_id"))
            return

        text = message.get("text", "").strip()
        if not text:
            return

        self._last_message_text = text

        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in self.commands:
                print(f"[cmd] {cmd} from user", flush=True)
                self.commands[cmd]()
            else:
                print(f"[cmd] Unknown command: {cmd}", flush=True)
                self.send(f"❓ Unknown command: {cmd}\nType /help for available commands")
        else:
            print(f"[claude] Free text received: \"{text[:80]}{'...' if len(text) > 80 else ''}\"", flush=True)
            self._handle_claude_message(text)

    def _handle_callback(self, callback: dict):
        """Handle inline keyboard button presses."""
        callback_id = callback.get("id", "")
        data = callback.get("data", "")
        message_id = callback.get("message", {}).get("message_id")
        print(f"[cb] Button pressed: {data}", flush=True)

        self.answer_callback_query(callback_id)

        if not message_id:
            return

        if data.startswith("claude:"):
            self._handle_claude_callback(data, message_id)
            return

        self.on_domain_callback(data, message_id)

    def _register_commands(self):
        """Register bot commands with Telegram (updates BotFather menu)."""
        bot_commands = [
            {"command": "claude", "description": "Claude Code menu"},
            {"command": "help", "description": "Show help message"},
        ]
        for cmd, (_handler, desc) in self.domain_commands().items():
            bot_commands.append({"command": cmd.lstrip("/"), "description": desc})

        try:
            import requests as _requests

            for scope in [
                None,
                {"type": "all_private_chats"},
                {"type": "all_group_chats"},
                {"type": "all_chat_administrators"},
            ]:
                payload = {"scope": scope} if scope else {}
                dr = _requests.post(f"{self.base_url}/deleteMyCommands", json=payload, timeout=10)
                label = scope["type"] if scope else "default"
                print(f"[cmd-reg] deleteMyCommands({label}): {dr.status_code}", flush=True)

            url = f"{self.base_url}/setMyCommands"
            resp = _requests.post(url, json={"commands": bot_commands}, timeout=10)
            print(f"[cmd-reg] setMyCommands: {resp.status_code} {resp.json()}", flush=True)

            get_resp = _requests.post(f"{self.base_url}/getMyCommands", timeout=10)
            registered = get_resp.json()
            print(f"[cmd-reg] getMyCommands: {len(registered.get('result', []))} commands: {registered}", flush=True)

            if resp.status_code == 200 and resp.json().get("ok"):
                print(f"[i] Telegram bot commands registered ({len(bot_commands)} commands)", flush=True)
            else:
                print(f"[!] Failed to register commands: {resp.status_code} {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"[!] Failed to register commands: {e}", flush=True)
