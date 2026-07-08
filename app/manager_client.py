"""Fetch active providers from cheeko-backend manager-api (ADR-0002).
One fetch serves both STT and moderation blocks; cached with a TTL and
serving last-known-good on fetch failure so a manager-api outage degrades
to the cached/last-resort provider rather than blocking."""
import logging
import time

import httpx

from app import config
from app.stt_providers import ProviderConfig

logger = logging.getLogger(__name__)

_cache = {"data": None, "ts": 0.0}  # data: {"stt": cfg|None, "moderation": cfg|None}


def _block(d: dict, key: str) -> ProviderConfig | None:
    blk = d.get(key)
    if not blk or not blk.get("provider"):
        return None
    return ProviderConfig(
        provider=str(blk["provider"]).lower(),
        model=blk.get("model") or "",
        language=blk.get("language") or "",
        api_key=blk.get("api_key") or "",
    )


def _parse(body: dict) -> dict:
    d = body.get("data") or body
    return {"stt": _block(d, "stt"), "moderation": _block(d, "moderation")}


async def _fetch(client: httpx.AsyncClient) -> dict:
    resp = await client.get(
        f"{config.MANAGER_API_BASE_URL}/providers/active",
        headers={"X-Service-Key": config.SERVICE_SECRET_KEY},
    )
    resp.raise_for_status()
    return _parse(resp.json())


async def _get_active(kind: str, client: httpx.AsyncClient | None,
                      now: float | None) -> ProviderConfig | None:
    if not config.MANAGER_API_BASE_URL:
        return None
    now = time.time() if now is None else now
    if _cache["data"] is not None and (now - _cache["ts"]) < config.STT_PROVIDER_TTL_S:
        return _cache["data"].get(kind)

    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        data = await _fetch(client)
        _cache["data"] = data
        _cache["ts"] = now
        return data.get(kind)
    except Exception as e:  # network, 5xx, parse — serve last-known-good
        cached = _cache["data"] or {}
        logger.warning("manager-api active-provider fetch failed (%s); using cache=%s",
                       e, cached.get(kind).provider if cached.get(kind) else None)
        return cached.get(kind)
    finally:
        if owns:
            await client.aclose()


async def get_active_stt(client: httpx.AsyncClient | None = None,
                         now: float | None = None) -> ProviderConfig | None:
    return await _get_active("stt", client, now)


async def get_active_moderation(client: httpx.AsyncClient | None = None,
                                now: float | None = None) -> ProviderConfig | None:
    return await _get_active("moderation", client, now)
