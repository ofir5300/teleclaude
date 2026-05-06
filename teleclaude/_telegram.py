"""Telegram HTTP client mixin — no bot/session state, just the API surface."""

import html as _html
import json
import time
from typing import Optional

_TG_MAX_LEN = 4000


def _escape_html(text: str) -> str:
    """Escape <, >, & so raw markdown/code doesn't break Telegram HTML parse_mode."""
    return _html.escape(text, quote=False)


class TelegramMixin:
    """HTTP methods for the Telegram Bot API. Expects self.token, self.chat_id, self.base_url."""

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str) -> bool:
        """Send a message. Tries HTML parse_mode first; falls back to plain text."""
        if not self.is_configured:
            return False
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            resp = __import__("requests").post(url, data=data, timeout=10)
            if resp.status_code == 200:
                return True
            print(f"[!] Telegram HTML send failed ({resp.status_code}), retrying as plain text", flush=True)
            data.pop("parse_mode")
            resp2 = __import__("requests").post(url, data=data, timeout=10)
            if resp2.status_code != 200:
                print(f"[!] Telegram plain send also failed ({resp2.status_code}): {resp2.text[:300]}", flush=True)
            return resp2.status_code == 200
        except Exception as e:
            print(f"[!] Telegram send failed: {e}", flush=True)
            return False

    def send_long(self, message: str, max_len: int = _TG_MAX_LEN) -> bool:
        """Send a long message, splitting into multiple messages at newline boundaries."""
        if len(message) <= max_len:
            return self.send(message)

        chunks = []
        remaining = message
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, max_len)
            if cut <= 0:
                cut = max_len
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")

        ok = True
        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(0.3)
            if not self.send(chunk):
                ok = False
        return ok

    def send_with_markup(self, message: str, reply_markup: dict) -> Optional[int]:
        """Send a message with an inline keyboard. Returns message_id on success."""
        if not self.is_configured:
            return None
        try:
            import requests as _requests

            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": json.dumps(reply_markup),
            }
            resp = _requests.post(url, data=payload, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id")
            print(f"[!] Telegram send_with_markup failed ({resp.status_code}): {resp.text[:300]}", flush=True)
        except Exception as e:
            print(f"[!] Telegram send_with_markup failed: {e}", flush=True)
        return None

    def edit_message(self, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message in-place (for inline keyboard drill-down)."""
        if not self.is_configured:
            return False
        try:
            import requests as _requests

            url = f"{self.base_url}/editMessageText"
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup)
            resp = _requests.post(url, data=payload, timeout=10)
            if resp.status_code != 200:
                print(f"[!] Telegram edit_message failed ({resp.status_code}): {resp.text[:300]}", flush=True)
            return resp.status_code == 200
        except Exception as e:
            print(f"[!] Telegram edit_message failed: {e}", flush=True)
            return False

    def answer_callback_query(self, callback_query_id: str) -> bool:
        """Acknowledge a callback query (removes the loading spinner on the button)."""
        try:
            import requests as _requests

            url = f"{self.base_url}/answerCallbackQuery"
            resp = _requests.post(url, data={"callback_query_id": callback_query_id}, timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def get_updates(self, timeout: int = 30) -> list:
        """Get new messages from Telegram via long-polling."""
        try:
            import requests as _requests

            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            }
            resp = _requests.get(url, params=params, timeout=timeout + 5)
            if resp.status_code == 200:
                return resp.json().get("result", [])
        except (ConnectionResetError, ConnectionError):
            time.sleep(2)
        except Exception as e:
            print(f"[!] Failed to get updates: {e}")
            time.sleep(2)
        return []
