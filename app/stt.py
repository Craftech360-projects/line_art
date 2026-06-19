import logging

import httpx

from app import config

logger = logging.getLogger(__name__)


async def transcribe(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str:
    """Transcribe audio bytes to text using the local Speaches server."""
    url = f"{config.SPEACHES_BASE_URL}/v1/audio/transcriptions"
    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {"model": config.SPEACHES_MODEL, "response_format": "json"}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(url, files=files, data=data)
        resp.raise_for_status()
        result = resp.json()
        text = result.get("text", "").strip()
        logger.info("Speaches transcription: '%s'", text)
        return text
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise RuntimeError(f"Speaches unavailable at {config.SPEACHES_BASE_URL}: {e}") from e
    finally:
        if owns_client:
            await client.aclose()
