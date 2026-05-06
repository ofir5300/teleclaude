"""Voice message → Whisper transcription → Claude."""

import os
import tempfile
import threading


class VoiceMixin:
    """Download Telegram voice/audio, transcribe via Whisper, route to Claude."""

    def _handle_voice_message(self, file_id: str):
        """Download and transcribe a Telegram voice message, then route to Claude."""
        if not file_id:
            self.send("Could not process voice message.")
            return

        self.send("Transcribing voice message...")

        def transcribe():
            try:
                import requests as _requests

                file_info = _requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}).json()
                file_path = file_info.get("result", {}).get("file_path", "")
                if not file_path:
                    self.send("Failed to get voice file from Telegram.")
                    return

                download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
                audio_data = _requests.get(download_url).content

                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    tmp.write(audio_data)
                    tmp_path = tmp.name

                if self._whisper_model is None:
                    import whisper

                    self._whisper_model = whisper.load_model("base")

                result = self._whisper_model.transcribe(tmp_path)
                text = result.get("text", "").strip()

                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

                if not text:
                    self.send("Could not transcribe audio (empty result).")
                    return

                self.send(f"Heard: <i>{text}</i>")
                self._handle_claude_message(text)

            except ImportError:
                self.send("whisper not installed. Run: pip install openai-whisper")
            except Exception as e:
                self.send(f"Transcription error: {str(e)[:500]}")

        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()
