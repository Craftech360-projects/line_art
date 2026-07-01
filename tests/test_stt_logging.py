import unittest
from unittest.mock import patch

from app import stt


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"text": "  hello world  "}


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return FakeResponse()


class TranscribeLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_groq_whisper_transcript(self):
        with patch.object(stt, "GROQ_API_KEY", "test-key"):
            with patch.object(stt.httpx, "AsyncClient", FakeAsyncClient):
                with self.assertLogs("app.stt", level="INFO") as logs:
                    text = await stt.transcribe(b"wav bytes")

        self.assertEqual(text, "hello world")
        self.assertIn("Groq Whisper transcript: 'hello world'", "\n".join(logs.output))
