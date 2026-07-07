"""Fetch the active STT provider from cheeko-backend manager-api (ADR-0002).
Caches with a TTL and serves last-known-good on fetch failure so a manager-api
outage degrades to the cached/last-resort provider rather than blocking."""
import logging
import time

import httpx

from app import config
from app.stt_providers import ProviderConfig

logger = logging.getLogger(__name__)

_cache = {"cfg": None, "ts": 0.0}


def _parse(body: dict) -> ProviderConfig | None:
    stt = (body.get("data") or body).get("stt")
    if not stt or not stt.get("provider"):
        return None
    return ProviderConfig(
        provider=str(stt["provider"]).lower(),
        model=stt.get("model") or "",
        language=stt.get("language") or "",
        api_key=stt.get("api_key") or "",
    )


async def _fetch(client: httpx.AsyncClient) -> ProviderConfig | None:
    resp = await client.get(
        f"{config.MANAGER_API_BASE_URL}/providers/active",
        headers={"X-Service-Key": config.SERVICE_SECRET_KEY},
    )
    resp.raise_for_status()
    return _parse(resp.json())


async def get_active_stt(client: httpx.AsyncClient | None = None,
                         now: float | None = None) -> ProviderConfig | None:
    if not config.MANAGER_API_BASE_URL:
        return None
    now = time.time() if now is None else now
    if _cache["cfg"] is not None and (now - _cache["ts"]) < config.STT_PROVIDER_TTL_S:
        return _cache["cfg"]

    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        cfg = await _fetch(client)
        if cfg is not None:
            _cache["cfg"] = cfg
            _cache["ts"] = now
        return cfg if cfg is not None else _cache["cfg"]
    except Exception as e:  # network, 5xx, parse — serve last-known-good
        logger.warning("manager-api active-provider fetch failed (%s); using cache=%s",
                       e, _cache["cfg"].provider if _cache["cfg"] else None)
        return _cache["cfg"]
    finally:
        if owns:
            await client.aclose()
