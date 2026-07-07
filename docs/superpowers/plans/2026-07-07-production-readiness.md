# Production Readiness (Multi-STT + Pilot Hardening) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make line_art production-ready for a tens-to-hundreds-device pilot: pluggable multi-provider STT with manager-api-driven selection + Groq fallback (ADR-0002), plus operational hardening (health check, safe deploys, error tracking, data hygiene, trust boundary).

**Architecture:** Part A refactors the single `stt.transcribe` dispatch into a provider-adapter registry (`stt_providers.py`) orchestrated by a resolver that reads the *active* STT provider from cheeko-backend's manager-api (cached, last-known-good) and falls back to a fixed env "last-resort" (Groq) on hard failures only. Part B adds a `/health` endpoint, a deploy health-gate with auto-rollback, Sentry-style error reporting, gates on-disk data capture, and an optional shared-secret on the `/ws` handshake.

**Tech Stack:** Python 3.11, FastAPI, httpx (async), Pillow, pytest (`asyncio_mode=auto`, `httpx.MockTransport`), pm2, GitHub Actions.

## Global Constraints

- Public API of `app.stt.transcribe(audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str` MUST stay unchanged — `device_protocol.handle_device_session` and `main.handle_audio_input` inject and call it as-is.
- Fallback triggers on **hard failures only**: connect error, timeout, HTTP 5xx, 429, 401/403 (and any non-2xx). A `200` with empty/whitespace text is **not** a failure — return `""` (the `MIN_UTTERANCE_FRAMES` guard is the no-speech backstop).
- Chain depth ≤ 2 (primary + last-resort), 30s per provider (keep `httpx` timeout at 30.0). Worst case 60s STT stays under the gateway's ~90s window.
- No new heavy dependencies. `httpx` (present) covers all HTTP. `sentry-sdk` is the only new dependency, and only in Part B Task 3.
- Match existing test style: `@pytest.mark.asyncio`, `monkeypatch.setattr(config, ...)`, `httpx.MockTransport`, dependency-injected `client=`.
- Every generated image / captured audio on disk is children's data — default all disk capture OFF in prod.

---

## File Structure

- **Create** `app/stt_providers.py` — `ProviderConfig`, `STTHardFailure`, per-vendor adapters (groq, speaches, deepgram, sarvam), adapter registry, `transcribe_with()`.
- **Create** `app/manager_client.py` — `get_active_stt()` with TTL cache + last-known-good, service-key auth.
- **Modify** `app/stt.py` — becomes the orchestrator: `transcribe()` resolves the chain and tries providers in order. Moves the groq/speaches HTTP bodies into `stt_providers.py`.
- **Modify** `app/config.py` — manager-api + last-resort + TTL + capture-gate + Sentry + WS-secret settings.
- **Modify** `app/main.py` — `/health` route; Sentry init in `lifespan`; gate `_save_input_wav`; pass WS secret check.
- **Modify** `app/device_protocol.py` — optional WS shared-secret check on `hello`.
- **Modify** `app/image_gen.py` — gate `_save_copies` behind `config.SAVE_GENERATED_IMAGES`.
- **Modify** `deploy/deploy.sh` — health-gate + auto-rollback.
- **Modify** `.env.example`, `README.md` — new settings.
- **Create/Modify** tests: `tests/test_stt_providers.py`, `tests/test_manager_client.py`, `tests/test_stt_fallback.py`, `tests/test_health.py`, `tests/test_save_gating.py`, `tests/test_ws_secret.py`; update `tests/test_providers.py`.

---

## PART A — Multi-Provider STT (implements ADR-0002)

### Task A1: Provider config + adapter registry with groq & speaches

**Files:**
- Create: `app/stt_providers.py`
- Test: `tests/test_stt_providers.py`

**Interfaces:**
- Produces:
  - `ProviderConfig` dataclass: `provider: str, model: str, language: str, api_key: str`
  - `class STTHardFailure(Exception)`
  - `async def transcribe_with(cfg: ProviderConfig, audio_bytes: bytes, client: httpx.AsyncClient | None = None) -> str`
  - registry `ADAPTERS: dict[str, callable]` keyed by provider name

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stt_providers.py
import httpx
import pytest
from app import stt_providers as sp
from app.stt_providers import ProviderConfig, STTHardFailure


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)


@pytest.mark.asyncio
async def test_groq_adapter_returns_stripped_text():
    cfg = ProviderConfig(provider="groq", model="whisper-large-v3", language="", api_key="k")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer k"
        assert b"whisper-large-v3" in request.content
        return httpx.Response(200, json={"text": "  a cat  "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a cat"


@pytest.mark.asyncio
async def test_groq_adapter_empty_text_is_not_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        return httpx.Response(200, json={"text": "   "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == ""


@pytest.mark.asyncio
async def test_groq_adapter_429_is_hard_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)


@pytest.mark.asyncio
async def test_connect_error_is_hard_failure():
    cfg = ProviderConfig(provider="groq", model="m", language="", api_key="k")

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)


@pytest.mark.asyncio
async def test_unknown_provider_is_hard_failure():
    cfg = ProviderConfig(provider="nope", model="m", language="", api_key="k")
    with pytest.raises(STTHardFailure, match="no adapter"):
        await sp.transcribe_with(cfg, b"wav")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stt_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.stt_providers'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/stt_providers.py
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


ADAPTERS = {
    "groq": _groq,
    "speaches": _speaches,
}


async def transcribe_with(cfg: ProviderConfig, audio_bytes: bytes, client=None) -> str:
    adapter = ADAPTERS.get(cfg.provider)
    if adapter is None:
        raise STTHardFailure(f"no adapter for provider {cfg.provider!r}")
    text = await adapter(cfg, audio_bytes, client)
    logger.info("STT[%s] -> %r", cfg.provider, text)
    return text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stt_providers.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app/stt_providers.py tests/test_stt_providers.py
git commit -m "feat(stt): provider adapter registry with groq + speaches"
```

---

### Task A2: Deepgram adapter

**Files:**
- Modify: `app/stt_providers.py`
- Test: `tests/test_stt_providers.py`

**Interfaces:**
- Produces: `_deepgram` adapter registered under `"deepgram"`. Deepgram prerecorded API: `POST https://api.deepgram.com/v1/listen`, header `Authorization: Token <key>`, raw audio body `Content-Type: audio/wav`, query `model`, optional `language`, `smart_format=true`. Transcript at `results.channels[0].alternatives[0].transcript`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stt_providers.py
@pytest.mark.asyncio
async def test_deepgram_adapter_parses_transcript():
    cfg = ProviderConfig(provider="deepgram", model="nova-2", language="en", api_key="dk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Token dk"
        assert "model=nova-2" in str(request.url)
        assert "language=en" in str(request.url)
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [{"transcript": "  a dog  "}]}]}
        })

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "a dog"


@pytest.mark.asyncio
async def test_deepgram_5xx_is_hard_failure():
    cfg = ProviderConfig(provider="deepgram", model="nova-2", language="", api_key="dk")

    def handler(request):
        return httpx.Response(503, text="unavailable")

    async with _client(handler) as c:
        with pytest.raises(STTHardFailure):
            await sp.transcribe_with(cfg, b"wav", c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stt_providers.py -k deepgram -v`
Expected: FAIL with `STTHardFailure: no adapter for provider 'deepgram'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/stt_providers.py — add adapter and register it
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
        alts = resp.json().get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])
        return (alts[0].get("transcript", "") if alts else "").strip()
    return await _with_client(client, call)


ADAPTERS["deepgram"] = _deepgram
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stt_providers.py -k deepgram -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/stt_providers.py tests/test_stt_providers.py
git commit -m "feat(stt): deepgram adapter"
```

---

### Task A3: Sarvam adapter (saarika STT + saaras translate)

**Files:**
- Modify: `app/stt_providers.py`
- Test: `tests/test_stt_providers.py`

**Interfaces:**
- Produces: `_sarvam` adapter under `"sarvam"`. Header `api-subscription-key: <key>`, multipart `file` + `model`. Model prefix `saaras*` → `POST https://api.sarvam.ai/speech-to-text-translate` (auto-detect, outputs English, no language_code). Otherwise (`saarika*`) → `POST https://api.sarvam.ai/speech-to-text` with `language_code` (defaults to `unknown` for auto-detect when `cfg.language` is empty). Transcript at JSON key `transcript`.

> Verification step included below — confirm the endpoint paths/field names against current Sarvam docs before merging; the code is concrete but the vendor contract should be re-checked.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_stt_providers.py
@pytest.mark.asyncio
async def test_sarvam_saaras_uses_translate_endpoint():
    cfg = ProviderConfig(provider="sarvam", model="saaras:v3", language="", api_key="sk")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech-to-text-translate"
        assert request.headers.get("api-subscription-key") == "sk"
        return httpx.Response(200, json={"transcript": " ek billi "})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "ek billi"


@pytest.mark.asyncio
async def test_sarvam_saarika_uses_stt_endpoint_with_language():
    cfg = ProviderConfig(provider="sarvam", model="saarika:v2", language="hi-IN", api_key="sk")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"transcript": "namaste"})

    async with _client(handler) as c:
        assert await sp.transcribe_with(cfg, b"wav", c) == "namaste"
    assert seen["path"] == "/speech-to-text"
    assert b"hi-IN" in seen["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stt_providers.py -k sarvam -v`
Expected: FAIL with `STTHardFailure: no adapter for provider 'sarvam'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/stt_providers.py — add adapter and register it
async def _sarvam(cfg: ProviderConfig, audio: bytes, client=None) -> str:
    model = cfg.model or "saarika:v2"
    files = {"file": ("audio.wav", audio, "audio/wav")}
    data = {"model": model}
    if model.startswith("saaras"):
        url = "https://api.sarvam.ai/speech-to-text-translate"
    else:
        url = "https://api.sarvam.ai/speech-to-text"
        data["language_code"] = cfg.language or "unknown"
    async def call(c: httpx.AsyncClient) -> str:
        resp = await c.post(url, headers={"api-subscription-key": cfg.api_key},
                            files=files, data=data)
        _check(resp)
        return resp.json().get("transcript", "").strip()
    return await _with_client(client, call)


ADAPTERS["sarvam"] = _sarvam
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stt_providers.py -k sarvam -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/stt_providers.py tests/test_stt_providers.py
git commit -m "feat(stt): sarvam adapter (saarika stt + saaras translate)"
```

---

### Task A4: Manager-api active-provider client (TTL cache + last-known-good)

**Files:**
- Create: `app/manager_client.py`
- Modify: `app/config.py`
- Test: `tests/test_manager_client.py`

**Interfaces:**
- Consumes: `ProviderConfig` (Task A1); `config.MANAGER_API_BASE_URL`, `config.SERVICE_SECRET_KEY`, `config.STT_PROVIDER_TTL_S`.
- Produces: `async def get_active_stt(client=None, now: float | None = None) -> ProviderConfig | None`, and module-level cache `_cache`. Returns `None` when no base URL is configured or the fetch fails on a cold cache. `GET {base}/providers/active` with header `X-Service-Key`; response envelope `{...,"data":{"stt":{"provider","model","language","api_key"}}}` (also accepts an un-enveloped `{"stt":...}`).

- [ ] **Step 1: Add config settings**

```python
# app/config.py — append after the existing settings
# --- Manager-api STT provider selection (ADR-0002) ---
# Base URL of cheeko-backend manager-api. Empty => skip manager fetch, use last-resort only.
MANAGER_API_BASE_URL = os.environ.get("MANAGER_API_BASE_URL", "").rstrip("/")
# Service key for backend-to-backend auth (X-Service-Key -> requireAdmin god-mode).
SERVICE_SECRET_KEY = os.environ.get("SERVICE_SECRET_KEY", "")
# How long line_art caches the active provider before refetching (seconds).
STT_PROVIDER_TTL_S = float(os.environ.get("STT_PROVIDER_TTL_S", "300"))
# Fixed env last-resort provider used when the active provider can't serve.
STT_LAST_RESORT_PROVIDER = os.environ.get("STT_LAST_RESORT_PROVIDER", "groq").lower()
# Extra keys so deepgram/sarvam can be the last-resort or used in dev.
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_manager_client.py
import httpx
import pytest
from app import manager_client as mc
from app import config


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)


@pytest.fixture(autouse=True)
def _reset_cache_and_config(monkeypatch):
    mc._cache["cfg"] = None
    mc._cache["ts"] = 0.0
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "http://mgr")
    monkeypatch.setattr(config, "SERVICE_SECRET_KEY", "svc")
    monkeypatch.setattr(config, "STT_PROVIDER_TTL_S", 300.0)


@pytest.mark.asyncio
async def test_fetches_and_maps_active_stt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/providers/active"
        assert request.headers.get("x-service-key") == "svc"
        return httpx.Response(200, json={"data": {"stt": {
            "provider": "deepgram", "model": "nova-2", "language": "en", "api_key": "dk"}}})

    async with _client(handler) as c:
        cfg = await mc.get_active_stt(c, now=1000.0)
    assert (cfg.provider, cfg.model, cfg.api_key) == ("deepgram", "nova-2", "dk")


@pytest.mark.asyncio
async def test_uses_cache_within_ttl_without_refetch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"stt": {
            "provider": "groq", "model": "m", "language": "", "api_key": "k"}})

    async with _client(handler) as c:
        await mc.get_active_stt(c, now=1000.0)
        await mc.get_active_stt(c, now=1100.0)  # within 300s TTL
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_serves_last_known_good_on_fetch_error():
    async with _client(lambda r: httpx.Response(200, json={"stt": {
            "provider": "groq", "model": "m", "language": "", "api_key": "k"}})) as c:
        first = await mc.get_active_stt(c, now=1000.0)
    # cache now populated; a later fetch that errors must return the cached cfg
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    async with _client(boom) as c2:
        second = await mc.get_active_stt(c2, now=9999.0)  # past TTL -> refetch -> error
    assert second.provider == first.provider == "groq"


@pytest.mark.asyncio
async def test_returns_none_when_no_base_url(monkeypatch):
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "")
    assert await mc.get_active_stt(now=1000.0) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_manager_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.manager_client'`

- [ ] **Step 4: Write minimal implementation**

```python
# app/manager_client.py
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_manager_client.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add app/manager_client.py app/config.py tests/test_manager_client.py
git commit -m "feat(stt): manager-api active-provider client with TTL cache + last-known-good"
```

---

### Task A5: Orchestrate transcribe() — resolve chain + hard-failure fallback

**Files:**
- Modify: `app/stt.py`
- Modify: `tests/test_providers.py` (update to new internals)
- Modify: `tests/test_stt.py` (update to new internals)
- Test: `tests/test_stt_fallback.py`

**Interfaces:**
- Consumes: `stt_providers.transcribe_with`, `stt_providers.STTHardFailure`, `stt_providers.ProviderConfig`, `manager_client.get_active_stt`, `config.STT_LAST_RESORT_PROVIDER`, `config.GROQ_MODEL/GROQ_API_KEY/DEEPGRAM_API_KEY/SARVAM_API_KEY/SPEACHES_BASE_URL/SPEACHES_MODEL`.
- Produces: unchanged public `async def transcribe(audio_bytes, client=None) -> str`; internal `_last_resort_config() -> ProviderConfig`, `async def _resolve_chain(client) -> list[ProviderConfig]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stt_fallback.py
import pytest
from app import stt, config
from app.stt_providers import ProviderConfig, STTHardFailure


@pytest.mark.asyncio
async def test_primary_success_no_fallback(monkeypatch):
    primary = ProviderConfig("deepgram", "nova-2", "", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "hello"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "hello"
    assert calls == ["deepgram"]  # last-resort never tried


@pytest.mark.asyncio
async def test_hard_failure_falls_to_last_resort(monkeypatch):
    primary = ProviderConfig("deepgram", "nova-2", "", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_MODEL", "whisper-large-v3")
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        if cfg.provider == "deepgram":
            raise STTHardFailure("429")
        return "recovered"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "recovered"
    assert calls == ["deepgram", "groq"]


@pytest.mark.asyncio
async def test_empty_text_is_returned_not_retried(monkeypatch):
    primary = ProviderConfig("groq", "m", "", "gk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "   "
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == ""
    assert calls == ["groq"]  # empty is a terminal no-speech, not a fallback trigger


@pytest.mark.asyncio
async def test_no_active_provider_uses_last_resort(monkeypatch):
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(None))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    async def fake_tw(cfg, audio, client=None):
        return f"via-{cfg.provider}"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "via-groq"


async def _async(value):
    return value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stt_fallback.py -v`
Expected: FAIL (`AttributeError: module 'app.stt' has no attribute 'manager_client'`)

- [ ] **Step 3: Rewrite `app/stt.py` as the orchestrator**

```python
# app/stt.py
import logging

from app import config
from app import manager_client
from app import stt_providers

logger = logging.getLogger(__name__)


def _last_resort_config() -> stt_providers.ProviderConfig:
    p = config.STT_LAST_RESORT_PROVIDER
    if p == "groq":
        return stt_providers.ProviderConfig("groq", config.GROQ_MODEL, "", config.GROQ_API_KEY)
    if p == "deepgram":
        return stt_providers.ProviderConfig("deepgram", "nova-2", "", config.DEEPGRAM_API_KEY)
    if p == "sarvam":
        return stt_providers.ProviderConfig("sarvam", "saarika:v2", "", config.SARVAM_API_KEY)
    if p == "speaches":
        # api_key field carries the Speaches base URL for the local dev path.
        return stt_providers.ProviderConfig("speaches", config.SPEACHES_MODEL, "", config.SPEACHES_BASE_URL)
    return stt_providers.ProviderConfig("groq", config.GROQ_MODEL, "", config.GROQ_API_KEY)


async def _resolve_chain(client=None) -> list[stt_providers.ProviderConfig]:
    chain: list[stt_providers.ProviderConfig] = []
    active = await manager_client.get_active_stt(client=client)
    if active is not None:
        chain.append(active)
    last_resort = _last_resort_config()
    if not chain or chain[0].provider != last_resort.provider:
        chain.append(last_resort)  # depth <= 2
    return chain


async def transcribe(audio_bytes: bytes, client=None) -> str:
    """Transcribe audio to text via the active provider, falling back to the
    env last-resort on HARD failures only. Empty text is returned as-is."""
    chain = await _resolve_chain(client)
    last_exc: Exception | None = None
    for cfg in chain:
        try:
            return await stt_providers.transcribe_with(cfg, audio_bytes, client)
        except stt_providers.STTHardFailure as e:
            last_exc = e
            logger.warning("STT provider %s hard-failed: %s", cfg.provider, e)
    raise RuntimeError(f"All STT providers failed: {last_exc}")
```

- [ ] **Step 4: Update the now-obsolete tests**

Replace the STT cases in `tests/test_providers.py` (the four `test_stt_*` / `_transcribe_*` cases at lines 6-24) with a single chain-shape test; leave the IMAGE_BACKEND tests untouched:

```python
# tests/test_providers.py — replace the two STT tests at the top
@pytest.mark.asyncio
async def test_last_resort_config_defaults_to_groq(monkeypatch):
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_MODEL", "whisper-large-v3")
    cfg = stt._last_resort_config()
    assert cfg.provider == "groq" and cfg.api_key == "gk"
```

Delete `tests/test_stt.py` (its groq-specific HTTP assertions now live in `tests/test_stt_providers.py::test_groq_adapter_*`):

```bash
git rm tests/test_stt.py
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all green; no reference to `stt._transcribe_groq`/`_transcribe_speaches` remains)

- [ ] **Step 6: Commit**

```bash
git add app/stt.py tests/test_stt_fallback.py tests/test_providers.py
git commit -m "refactor(stt): orchestrate active-provider chain with hard-failure fallback (ADR-0002)"
```

---

### Task A6: Env + docs for STT provider selection

**Files:**
- Modify: `.env.example`, `README.md`

- [ ] **Step 1: Append to `.env.example`**

```bash
# --- Multi-provider STT (ADR-0002) ---
# Manager-api supplies the ACTIVE provider; leave base URL empty to use last-resort only.
MANAGER_API_BASE_URL=http://localhost:3000
SERVICE_SECRET_KEY=your-service-secret-key
STT_PROVIDER_TTL_S=300
# Fixed fallback used on hard failure / manager-api down / unknown provider.
STT_LAST_RESORT_PROVIDER=groq
# Keys used only when that provider is the last-resort (primary keys come from manager-api).
DEEPGRAM_API_KEY=
SARVAM_API_KEY=
```

- [ ] **Step 2: Update the README `Configuration` section** — add a "Multi-provider STT" note pointing to `docs/adr/0002-stt-provider-selection-via-manager-api.md`, stating: primary provider comes from manager-api `GET /providers/active`, cached `STT_PROVIDER_TTL_S`, fallback to `STT_LAST_RESORT_PROVIDER` on hard failures only.

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs(stt): document manager-api provider selection + fallback"
```

---

## PART B — Pilot Hardening

### Task B1: `GET /health` liveness endpoint

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces: `GET /health` → `200 {"status": "ok"}`. Liveness only (does not probe providers — a provider outage must not mark the box unhealthy and trigger a rollback).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health.py
from fastapi.testclient import TestClient
from app.main import app


def test_health_returns_ok():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_health.py -v`
Expected: FAIL (404)

- [ ] **Step 3: Add the route in `app/main.py`** (after `app = FastAPI(...)`/`app.mount(...)`)

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_health.py
git commit -m "feat(ops): add GET /health liveness endpoint"
```

---

### Task B2: Gate on-disk capture behind config (default off)

**Files:**
- Modify: `app/config.py`, `app/image_gen.py`, `app/main.py`
- Test: `tests/test_save_gating.py`

**Interfaces:**
- Consumes: `config.SAVE_GENERATED_IMAGES` (bool, default False).
- Produces: `image_gen._save_copies` and `main._save_input_wav` are no-ops unless the flag is on. (`SAVE_DEVICE_AUDIO`/`SAVE_INPUT_AUDIO` already gate the audio dumps; this closes the image path.)

- [ ] **Step 1: Add config flag** — append to `app/config.py`:

```python
# Save every generated image to generated_images/ (children's data — default OFF in prod).
SAVE_GENERATED_IMAGES = os.environ.get("SAVE_GENERATED_IMAGES", "").lower() in ("1", "true", "yes")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_save_gating.py
import pytest
from app import image_gen, config


def test_save_copies_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAVE_GENERATED_IMAGES", False)
    monkeypatch.setattr(image_gen, "_IMAGE_DIR", tmp_path / "gen")
    image_gen._save_copies("a cat", b"full", b"mono")
    assert not (tmp_path / "gen").exists()  # nothing written


def test_save_copies_writes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAVE_GENERATED_IMAGES", True)
    monkeypatch.setattr(image_gen, "_IMAGE_DIR", tmp_path / "gen")
    image_gen._save_copies("a cat", b"full", b"mono")
    assert list((tmp_path / "gen").glob("*.png"))  # files written
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_save_gating.py -v`
Expected: FAIL (`test_save_copies_noop_when_disabled` — dir gets created)

- [ ] **Step 4: Guard `_save_copies`** — add as the first lines of `image_gen._save_copies`:

```python
    if not config.SAVE_GENERATED_IMAGES:
        return
```

Also guard `main._save_input_wav` — add as its first lines:

```python
    if not SAVE_INPUT_AUDIO:
        return
```

(`SAVE_INPUT_AUDIO` already exists in `main.py`; this makes the guard explicit inside the helper too.)

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_save_gating.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/image_gen.py app/main.py tests/test_save_gating.py
git commit -m "feat(ops): gate on-disk image/audio capture off by default (kids' data hygiene)"
```

---

### Task B3: Sentry-style error reporting

**Files:**
- Modify: `requirements.txt`, `app/config.py`, `app/main.py`
- Test: manual (SDK init is config-driven; no unit test — a no-DSN init must be a safe no-op)

**Interfaces:**
- Consumes: `config.SENTRY_DSN` (default ""). When empty, no SDK init (dev/test unaffected).

- [ ] **Step 1: Add dependency** — append to `requirements.txt`:

```
sentry-sdk>=2.0.0
```

- [ ] **Step 2: Add config** — append to `app/config.py`:

```python
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
SENTRY_ENV = os.environ.get("SENTRY_ENV", "production")
```

- [ ] **Step 3: Init in `main.py`** — near the top of `lifespan`, before the `logger.info("Server ready...")` line:

```python
    if config.SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.init(dsn=config.SENTRY_DSN, environment=config.SENTRY_ENV,
                        traces_sample_rate=0.0)
        logger.info("Sentry error reporting enabled (env=%s)", config.SENTRY_ENV)
```

- [ ] **Step 4: Report handled failures** — in `device_protocol._generate_imagine_and_send` and `_transcribe_and_prompt`, and in `main.handle_text_input`/`handle_audio_input`, the existing `logger.exception(...)` calls are auto-captured by Sentry's logging integration for unhandled paths. Add explicit capture where errors are swallowed into device messages — in `device_protocol._generate_imagine_and_send`'s `except` block, after `logger.exception(...)`:

```python
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:
            pass
```

- [ ] **Step 5: Verify no-DSN init is a safe no-op**

Run: `python -c "import os; os.environ.pop('SENTRY_DSN', None); from app.main import app; print('ok')"`
Expected: prints `ok` with no Sentry network calls.

- [ ] **Step 6: Verify the suite still passes**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add requirements.txt app/config.py app/main.py app/device_protocol.py
git commit -m "feat(ops): optional Sentry error reporting (no-op without DSN)"
```

> **Reuse check before merge:** grep cheeko-backend for an existing Sentry project/DSN convention (`grep -ri sentry` in the manager-api) and reuse that project rather than creating a new one.

---

### Task B4: Deploy health-gate + auto-rollback

**Files:**
- Modify: `deploy/deploy.sh`
- Test: manual (shell; can't unit-test a server deploy)

**Interfaces:**
- Consumes: `GET /health` (Task B1) on the app's local port (default 8090). `PORT` env optional.

- [ ] **Step 1: Rewrite `deploy/deploy.sh`**

```bash
#!/usr/bin/env bash
# Server-side deploy for line_art FastAPI app. Source is already rsynced/pulled by CI.
# Health-gates the reload and auto-rolls-back to the previous commit on failure.
set -euo pipefail
cd /opt/line_art

PORT="${PORT:-8090}"
PREV_SHA="$(git rev-parse HEAD)"

echo "==> pull latest main (prev=$PREV_SHA)"
git fetch origin main
git reset --hard origin/main

reload() {
  echo "==> venv + deps"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  echo "==> pm2 reload"
  pm2 startOrReload /opt/ecosystem.config.js --only lineart --update-env
  pm2 save
}

healthy() {
  for i in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

reload
if healthy; then
  echo "==> line_art deploy healthy"
else
  echo "!! health check FAILED — rolling back to $PREV_SHA"
  git reset --hard "$PREV_SHA"
  reload
  healthy && echo "==> rolled back to previous healthy revision" || echo "!! rollback also unhealthy — manual intervention needed"
  exit 1
fi
```

- [ ] **Step 2: Manual verification (staging or the pilot box)**

1. Deploy a good commit → expect `line_art deploy healthy`, `curl :8090/health` = 200.
2. Deploy a deliberately-broken commit (e.g. syntax error) → expect `health check FAILED — rolling back`, exit non-zero, and `/health` back to 200 on the previous revision.

- [ ] **Step 3: Commit**

```bash
git add deploy/deploy.sh
git commit -m "feat(ops): health-gate deploy with auto-rollback to previous revision"
```

---

### Task B5: Optional shared-secret on the `/ws` handshake (trust boundary)

**Files:**
- Modify: `app/config.py`, `app/device_protocol.py`
- Test: `tests/test_ws_secret.py`

**Interfaces:**
- Consumes: `config.WS_SHARED_SECRET` (default ""). When set, the device/gateway `hello` must carry a matching `auth` field; mismatch closes the session before any work. When empty, behavior is unchanged (relies on network isolation).
- Produces: `device_protocol.handle_device_session` rejects unauthenticated `hello` when the secret is configured.

> This is the belt-and-suspenders for the OPEN trust-boundary item. **Primary action remains:** confirm port 8090 is reachable only by the gateway (private subnet / security group). Setting `WS_SHARED_SECRET` is the safeguard when isolation can't be confirmed. Once decided, record it as ADR-0003.

- [ ] **Step 1: Add config** — append to `app/config.py`:

```python
# If set, /ws device sessions must present a matching `auth` in the hello. Empty =>
# rely on network isolation (see ADR-0003). The gateway must send the same value.
WS_SHARED_SECRET = os.environ.get("WS_SHARED_SECRET", "")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_ws_secret.py
import pytest
from app import device_protocol as dp
from app import config


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def close(self, code=1008):
        self.closed = code


@pytest.mark.asyncio
async def test_hello_rejected_on_bad_secret(monkeypatch):
    monkeypatch.setattr(config, "WS_SHARED_SECRET", "s3cret")
    ws = _FakeWS()
    await dp.handle_device_session(ws, {"type": "hello", "auth": "wrong"})
    assert ws.closed == 1008
    assert not ws.sent  # no hello_reply issued


@pytest.mark.asyncio
async def test_hello_accepted_with_good_secret(monkeypatch):
    monkeypatch.setattr(config, "WS_SHARED_SECRET", "s3cret")
    ws = _FakeWS()
    await dp.handle_device_session(ws, {"type": "hello", "auth": "s3cret"})
    assert ws.closed is None
    assert ws.sent and ws.sent[0].get("type") == "hello"  # hello_reply sent
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_ws_secret.py -v`
Expected: FAIL (`test_hello_rejected_on_bad_secret` — session proceeds, no close)

- [ ] **Step 4: Add the guard** — at the very start of `device_protocol.handle_device_session`, before `session_id = uuid.uuid4().hex`:

```python
    from app import config as _cfg
    if _cfg.WS_SHARED_SECRET and first_message.get("auth") != _cfg.WS_SHARED_SECRET:
        logger.warning("Rejected device hello: bad/missing auth")
        await ws.close(code=1008)
        return
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_ws_secret.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Full suite + commit**

```bash
python -m pytest -q
git add app/config.py app/device_protocol.py tests/test_ws_secret.py
git commit -m "feat(ops): optional shared-secret on /ws handshake (trust boundary)"
```

---

## Post-Implementation

- [ ] **Verify trust boundary (blocking for pilot):** confirm port 8090 is reachable only by the gateway. If confirmed isolation-only, write `docs/adr/0003-*` recording "line_art relies on network isolation, not app auth." If not confirmable, set `WS_SHARED_SECRET` in prod + gateway and record that instead.
- [ ] **Update `.env` on the pilot box** with `MANAGER_API_BASE_URL`, `SERVICE_SECRET_KEY`, `SENTRY_DSN`, and (if used) `WS_SHARED_SECRET`. `SAVE_GENERATED_IMAGES` stays unset (off).
- [ ] **Full regression:** `python -m pytest -q` green; manual gateway → device round-trip on staging.

## Self-Review Notes

- Public `transcribe(audio_bytes, client=None)` signature preserved (A5) — `device_protocol`/`main` untouched on that path. ✓
- Hard-failure-only fallback + empty-text-returned covered by `test_stt_fallback.py`. ✓
- Chain depth ≤ 2 enforced in `_resolve_chain` (dedupes when active == last-resort). ✓
- Unknown provider → `STTHardFailure` → falls to last-resort (A1 test + A5 chain). ✓
- Kids'-data hygiene: images gated (B2), audio already gated. ✓
- Deploy safety depends on `/health` (B1 precedes B4). ✓
- Type consistency: `ProviderConfig(provider, model, language, api_key)` used identically across A1/A4/A5. ✓
