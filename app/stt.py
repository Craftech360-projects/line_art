import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def transcribe(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Transcribe audio bytes to text using the Groq Whisper API."""
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")

    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {"model": config.GROQ_MODEL, "response_format": "json"}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(GROQ_API_URL, headers=headers, files=files, data=data)
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
        logger.info("Groq transcription: '%s'", text)
        return text
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RuntimeError(f"Groq unavailable: {e}") from e
    finally:
        if owns_client:
            await client.aclose()
