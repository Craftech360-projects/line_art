import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def transcribe(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Transcribe audio bytes to text using the configured STT backend."""
    if config.STT_BACKEND == "local":
        return await _transcribe_speaches(audio_bytes, client)
    return await _transcribe_groq(audio_bytes, client)


async def _transcribe_groq(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Cloud STT via the Groq Whisper API."""
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


async def _transcribe_speaches(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Local STT via a Speaches (faster-whisper) server, OpenAI-compatible endpoint."""
    url = f"{config.SPEACHES_BASE_URL}/v1/audio/transcriptions"
    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {"model": config.SPEACHES_MODEL, "response_format": "json"}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(url, files=files, data=data)
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
        logger.info("Speaches transcription: '%s'", text)
        return text
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RuntimeError(f"Speaches unavailable at {config.SPEACHES_BASE_URL}: {e}") from e
    finally:
        if owns_client:
            await client.aclose()
