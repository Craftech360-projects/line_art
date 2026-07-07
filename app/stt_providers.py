"""STT provider adapters. Each adapter takes (ProviderConfig, audio_bytes, client)
and returns transcribed text, or raises STTHardFailure on a failure that should
trigger fallback. A 200 with empty text is NOT a failure (return "")."""
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    provider: str
    model: str
    language: str
    api_key: str


class STTHardFailure(Exception):
    """A provider failure that should advance the fallback chain."""


def _check(resp: httpx.Response) -> None:
    """Any non-2xx is a hard failure (5xx, 429, 401/403, and 4xx misconfig)."""
    if resp.status_code // 100 != 2:
        raise STTHardFailure(f"HTTP {resp.status_code}")


async def _with_client(client, coro_factory):
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        return await coro_factory(client)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.PoolTimeout, httpx.WriteTimeout) as e:
        raise STTHardFailure(f"transport error: {e}") from e
    finally:
        if owns:
            await client.aclose()


async def _groq(cfg: ProviderConfig, audio: bytes, client=None) -> str:
    async def call(c: httpx.AsyncClient) -> str:
        resp = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            files={"file": ("audio.wav", audio, "audio/wav")},
            data={"model": cfg.model or "whisper-large-v3", "response_format": "json"},
        )
        _check(resp)
        return resp.json().get("text", "").strip()
    return await _with_client(client, call)


async def _speaches(cfg: ProviderConfig, audio: bytes, client=None) -> str:
    # cfg.api_key holds the Speaches base URL for the local dev path.
    base = cfg.api_key or "http://localhost:8001"
    async def call(c: httpx.AsyncClient) -> str:
        resp = await c.post(
            f"{base}/v1/audio/transcriptions",
            files={"file": ("audio.wav", audio, "audio/wav")},
            data={"model": cfg.model, "response_format": "json"},
        )
        _check(resp)
        return resp.json().get("text", "").strip()
    return await _with_client(client, call)


async def _deepgram(cfg: ProviderConfig, audio: bytes, client=None) -> str:
    params = {"model": cfg.model or "nova-2", "smart_format": "true"}
    if cfg.language:
        params["language"] = cfg.language
    async def call(c: httpx.AsyncClient) -> str:
        resp = await c.post(
            "https://api.deepgram.com/v1/listen",
            headers={"Authorization": f"Token {cfg.api_key}",
                     "Content-Type": "audio/wav"},
            params=params,
            content=audio,
        )
        _check(resp)
        channels = resp.json().get("results", {}).get("channels") or []
        alts = (channels[0].get("alternatives") or []) if channels else []
        return (alts[0].get("transcript", "") if alts else "").strip()
    return await _with_client(client, call)


ADAPTERS = {
    "groq": _groq,
    "speaches": _speaches,
    "deepgram": _deepgram,
}


async def transcribe_with(cfg: ProviderConfig, audio_bytes: bytes, client=None) -> str:
    adapter = ADAPTERS.get(cfg.provider)
    if adapter is None:
        raise STTHardFailure(f"no adapter for provider {cfg.provider!r}")
    text = await adapter(cfg, audio_bytes, client)
    logger.info("STT[%s] -> %r", cfg.provider, text)
    return text
