# Image Provider Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make line_art's image-generation provider admin-switchable (hf / runware / fal) via a new `image_providers` DB table served by manager-api, mirroring the just-landed moderation provider pattern.

**Architecture:** Exact clone of the moderation-provider feature (plan 2026-07-08-moderation-provider-selection.md, all shipped): `image_providers` table → `image` block in `GET /providers/active` → line_art `manager_client.get_active_image()` (same single fetch/cache) → adapter chain in `app/image_gen.py` (manager active → env HF last resort). `IMAGE_BACKEND=comfyui` keeps working as the local-dev override; the imagine path's `fallback.jpg` remains the terminal fallback.

**Tech Stack:** Node/Express/Prisma (manager-api), Python/httpx/pytest (line_art), Supabase Postgres.

## Global Constraints

- Mirror the moderation/STT pattern exactly — same table shape, same service map, same route shape, same line_art chain semantics. No new abstractions.
- Provider names: `hf`, `runware`, `fal`. Variant rows (e.g. `runware_schnell`) route by base name before the first `_` (same rule as moderation).
- `_generate_image_bytes(prompt, width=None, height=None) -> bytes` keeps its signature; `IMAGE_BACKEND == "comfyui"` short-circuits to the local ComfyUI path unchanged; existing tests in `tests/test_providers.py` must keep passing unmodified (they monkeypatch `generate_with_huggingface` — that function must remain the HF adapter's core).
- On total chain failure raise (the imagine path upstream already serves `fallback.jpg`; the printer path surfaces the error — both unchanged).
- Runware facts (verified 2026-07-08): endpoint `POST https://api.runware.ai/v1`, Bearer auth, body = JSON array of tasks `{taskType:"imageInference", taskUUID, model, positivePrompt, width, height, steps:4, outputType:"base64Data", outputFormat:"PNG", deliveryMethod:"sync", numberResults:1}`; FLUX.1 schnell = `runware:100@1`, FLUX.2 klein 4B = `runware:400@4`. Width/height must be multiples of 64.
- fal facts: sync endpoint `POST https://fal.run/<model-path>` (e.g. `fal-ai/flux/schnell`), header `Authorization: Key <api_key>`, body `{"prompt": ..., "image_size": {"width": W, "height": H}}` → `{"images":[{"url": ...}]}`; fetch the URL for bytes.
- Seed rows: `hf` (model `black-forest-labs/FLUX.1-schnell`, **active**, priority 100 — preserves current behavior), `runware` (`runware:400@4`, 50), `fal` (`fal-ai/flux/schnell`, 40). All `api_key=''`.
- No secrets/DB URLs committed. Migration applied with `DIRECT_URL=<user-supplied> npx prisma db execute --file ...` (env-var form; `--url` flag is NOT supported by Prisma 7.4).
- Repos: manager-api `D:\cheeko-backend\main\manager-api-node` (branch `lineart_moderation`), line_art `D:\line_art` (branch `feat/moderation-providers`). Commit per task in its repo.

---

### Task 1: `image_providers` table (schema + migration + seed)

**Files:**
- Create: `D:\cheeko-backend\main\manager-api-node\prisma\migrations\20260708100000_add_image_providers\migration.sql`
- Modify: `D:\cheeko-backend\main\manager-api-node\prisma\schema.prisma` (insert after `model moderation_providers`, before `model tts_providers`)

**Interfaces:**
- Produces: Prisma delegate `prisma.image_providers`, columns identical to `moderation_providers`.

- [ ] **Step 1: Migration SQL**

```sql
-- prisma/migrations/20260708100000_add_image_providers/migration.sql
CREATE TABLE IF NOT EXISTS "image_providers" (
    "id"            BIGSERIAL PRIMARY KEY,
    "provider_name" TEXT NOT NULL,
    "api_key"       TEXT NOT NULL DEFAULT '',
    "model"         TEXT NOT NULL DEFAULT '',
    "is_active"     BOOLEAN NOT NULL DEFAULT false,
    "priority"      INTEGER NOT NULL DEFAULT 0,
    "config_json"   JSONB,
    "created_at"    TIMESTAMPTZ(6) DEFAULT now(),
    "updated_at"    TIMESTAMPTZ(6) DEFAULT now(),
    CONSTRAINT "image_providers_provider_name_key" UNIQUE ("provider_name")
);

CREATE INDEX IF NOT EXISTS "idx_image_active" ON "image_providers" ("is_active");
CREATE INDEX IF NOT EXISTS "idx_image_priority" ON "image_providers" ("priority" DESC);

-- hf starts active with empty key: line_art skips key-less active providers and
-- falls to its env HF token, so behavior is unchanged until the admin switches.
INSERT INTO "image_providers" ("provider_name", "model", "is_active", "priority") VALUES
    ('hf',      'black-forest-labs/FLUX.1-schnell', true,  100),
    ('runware', 'runware:400@4',                    false, 50),
    ('fal',     'fal-ai/flux/schnell',              false, 40)
ON CONFLICT ("provider_name") DO NOTHING;
```

- [ ] **Step 2: Prisma model** — append after `model moderation_providers`:

```prisma
model image_providers {
  id            BigInt    @id @default(autoincrement())
  provider_name String    @unique
  api_key       String    @default("")
  model         String    @default("")
  is_active     Boolean   @default(false)
  priority      Int       @default(0)
  config_json   Json?
  created_at    DateTime? @default(now()) @db.Timestamptz(6)
  updated_at    DateTime? @default(now()) @db.Timestamptz(6)

  @@index([is_active], map: "idx_image_active")
  @@index([priority(sort: Desc)], map: "idx_image_priority")
}
```

- [ ] **Step 3: Apply** — from the manager-api dir (bash): `DIRECT_URL="<user-supplied>" npx prisma db execute --file prisma/migrations/20260708100000_add_image_providers/migration.sql` → exit 0.
- [ ] **Step 4:** `npx prisma generate` → "Generated Prisma Client".
- [ ] **Step 5: Verify fail-loud** — `db execute` a `DO $$` block raising unless `count(*) >= 3` and `hf` is active in `image_providers`.
- [ ] **Step 6: Commit** — only the two files; message: `feat(providers): add image_providers table (hf/runware/fal)`

---

### Task 2: manager-api — serve + manage image providers

**Files:**
- Modify: `D:\cheeko-backend\main\manager-api-node\src\services\livekitProviders.service.js`
- Modify: `D:\cheeko-backend\main\manager-api-node\src\routes\livekitProviders.routes.js`
- Test: `D:\cheeko-backend\main\manager-api-node\tests\unit\livekit-providers.image.test.js`
- (Allowed minimal consequence: add `image_providers` mock stubs to existing test files whose mocked prisma now misses the new delegate — same as the moderation task did.)

**Interfaces:**
- Consumes: `prisma.image_providers` (Task 1).
- Produces: `GET /livekit/providers/active` gains `image: { provider, model, api_key } | null`; `PUT /livekit/providers/active/image` accepts `{ provider, model?, api_key?, priority? }`; generic `:type` routes accept `image`.

- [ ] **Step 1: Failing jest test** — clone `tests/unit/livekit-providers.moderation.test.js` (read it first), renaming moderation→image, delegate `image_providers`, sample row `{provider_name:'runware', model:'runware:400@4', api_key:'rk'}`, service fn `setActiveImageProvider`. Same 4 test shapes: active-block returned, null-when-none, deactivate+upsert, `updateProvider('image', ...)`. Remember the mock object needs ALL five delegates (llm/stt/tts/moderation/image) since `getActiveProviders`/`listProviders` now query five tables.
- [ ] **Step 2:** Run it → FAIL (`out.image` undefined, `setActiveImageProvider` not a function).
- [ ] **Step 3: Implement** — exact mirror of the moderation edits in the same file:
  - `providerModels.image = { delegate: 'image_providers', updateFields: { provider_name:'string', model:'string', api_key:'string', priority:'int', config_json:'json' } }`
  - `listProviders`: add `image` to the `Promise.all` + response.
  - `getActiveProviders`: add `image` findFirst to `Promise.all`, include in `pickLatestUpdatedAt([...])`, add response block `image: image ? { provider: image.provider_name, model: image.model || '', api_key: image.api_key || '' } : null`. Keep llm/stt/tts/moderation blocks byte-identical.
  - `setActiveImageProvider` — copy `setActiveModerationProvider` with `image_providers` delegate. Export it.
- [ ] **Step 4: Route** — after the moderation route in `livekitProviders.routes.js`, add `PUT /providers/active/image` calling `setActiveImageProvider` (same requireAdmin/asyncHandler/try-catch shape, message `'Image provider updated'`).
- [ ] **Step 5:** New test file passes; patch any existing test whose prisma mock now lacks `image_providers` (minimal stubs only, 0 deletions).
- [ ] **Step 6:** `npm test` — no NEW failures (environmental failures documented in report).
- [ ] **Step 7: Commit** — message: `feat(providers): image provider in active-providers API + PUT /providers/active/image`

---

### Task 3: line_art — `get_active_image()` in manager_client

**Files:**
- Modify: `D:\line_art\app\manager_client.py`
- Test: `D:\line_art\tests\test_manager_client.py` (append)

**Interfaces:**
- Produces: `async get_active_image(client=None, now=None) -> ProviderConfig | None`, sharing the existing fetch/cache.

- [ ] **Step 1: Failing tests** — append to `tests/test_manager_client.py`:

```python
@pytest.mark.asyncio
async def test_image_block_is_parsed():
    payload = {"data": {
        "stt": {"provider": "groq", "api_key": "sk1"},
        "image": {"provider": "runware", "model": "runware:400@4", "api_key": "rk"},
    }}
    async with _client(payload) as c:
        cfg = await manager_client.get_active_image(client=c, now=1000.0)
    assert cfg.provider == "runware"
    assert cfg.model == "runware:400@4"
    assert cfg.api_key == "rk"


@pytest.mark.asyncio
async def test_missing_image_block_returns_none():
    payload = {"data": {"stt": {"provider": "groq", "api_key": "sk1"}}}
    async with _client(payload) as c:
        assert await manager_client.get_active_image(client=c, now=1000.0) is None
```

- [ ] **Step 2:** Run → FAIL (no attribute `get_active_image`).
- [ ] **Step 3: Implement** — in `manager_client.py`: `_parse` returns `{"stt": ..., "moderation": ..., "image": _block(d, "image")}`; add

```python
async def get_active_image(client: httpx.AsyncClient | None = None,
                           now: float | None = None) -> ProviderConfig | None:
    return await _get_active("image", client, now)
```

(No other changes — `_get_active` and the `any(data.values())` cache guard already generalize.)
- [ ] **Step 4:** Focused file passes; full suite green.
- [ ] **Step 5: Commit** — `feat(image): fetch active image provider from manager-api (shared cache)`

---

### Task 4: line_art — image provider adapters + chain in image_gen

**Files:**
- Modify: `D:\line_art\app\image_gen.py`
- Test: `D:\line_art\tests\test_image_providers.py` (new)

**Interfaces:**
- Consumes: `manager_client.get_active_image()`; `ProviderConfig`; existing `generate_with_huggingface`, `comfy_client.generate_png`, `config.HF_API_TOKEN`, `config.HF_MODEL_URL`, `config.IMAGE_BACKEND`.
- Produces: `_generate_image_bytes(prompt, width=None, height=None) -> bytes` (same signature, now chain-driven); `generate_image_with(cfg, prompt, width, height, client=None) -> bytes`; `ImageGenUnavailable` exception; `IMAGE_ADAPTERS` dict.

- [ ] **Step 1: Failing tests** — create `tests/test_image_providers.py`:

```python
"""Image provider adapters + fallback chain."""
import base64
import json

import httpx
import pytest

from app import config, image_gen
from app.stt_providers import ProviderConfig

PNG1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    b"h6FO1AAAAABJRU5ErkJggg==")


@pytest.mark.asyncio
async def test_runware_adapter_posts_task_and_decodes_base64():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        body = json.loads(request.content)
        seen["task"] = body[0]
        return httpx.Response(200, json={"data": [
            {"taskType": "imageInference",
             "imageBase64Data": base64.b64encode(PNG1x1).decode()}]})
    cfg = ProviderConfig("runware", "runware:400@4", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1
    assert seen["host"] == "api.runware.ai"
    assert seen["task"]["model"] == "runware:400@4"
    assert seen["task"]["positivePrompt"] == "a cat"
    assert (seen["task"]["width"], seen["task"]["height"]) == (512, 384)


@pytest.mark.asyncio
async def test_fal_adapter_posts_prompt_then_downloads_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fal.run":
            assert request.headers["Authorization"] == "Key fk"
            assert request.url.path == "/fal-ai/flux/schnell"
            return httpx.Response(200, json={"images": [{"url": "https://cdn.fal.example/x.png"}]})
        return httpx.Response(200, content=PNG1x1)
    cfg = ProviderConfig("fal", "fal-ai/flux/schnell", "", "fk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1


@pytest.mark.asyncio
async def test_hf_adapter_uses_cfg_key_and_model():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, content=PNG1x1)
    cfg = ProviderConfig("hf", "black-forest-labs/FLUX.1-schnell", "", "hk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1
    assert seen["auth"] == "Bearer hk"
    assert seen["path"].endswith("black-forest-labs/FLUX.1-schnell")


@pytest.mark.asyncio
async def test_http_error_raises_image_gen_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    cfg = ProviderConfig("runware", "runware:400@4", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(image_gen.ImageGenUnavailable):
            await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)


@pytest.mark.asyncio
async def test_variant_provider_routes_by_base_name():
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return httpx.Response(200, json={"data": [
            {"imageBase64Data": base64.b64encode(PNG1x1).decode()}]})
    cfg = ProviderConfig("runware_schnell", "runware:100@1", "", "rk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await image_gen.generate_image_with(cfg, "a cat", 512, 384, client=c)
    assert out == PNG1x1 and seen["host"] == "api.runware.ai"


@pytest.mark.asyncio
async def test_chain_uses_manager_active_then_env_last_resort(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "hf")
    monkeypatch.setattr(config, "HF_API_TOKEN", "envtoken")
    async def active_runware(client=None, now=None):
        return ProviderConfig("runware", "runware:400@4", "", "rk-bad")
    monkeypatch.setattr(image_gen.manager_client, "get_active_image", active_runware)
    calls = []
    async def fake_gen(cfg, prompt, width=None, height=None, client=None):
        calls.append(cfg.provider)
        if cfg.provider == "runware":
            raise image_gen.ImageGenUnavailable("runware down")
        return PNG1x1
    monkeypatch.setattr(image_gen, "generate_image_with", fake_gen)
    out = await image_gen._generate_image_bytes("a cat", width=512, height=384)
    assert out == PNG1x1
    assert calls == ["runware", "hf"]


@pytest.mark.asyncio
async def test_chain_skips_keyless_active(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_BACKEND", "hf")
    monkeypatch.setattr(config, "HF_API_TOKEN", "envtoken")
    async def active_keyless(client=None, now=None):
        return ProviderConfig("hf", "black-forest-labs/FLUX.1-schnell", "", "")
    monkeypatch.setattr(image_gen.manager_client, "get_active_image", active_keyless)
    calls = []
    async def fake_gen(cfg, prompt, width=None, height=None, client=None):
        calls.append((cfg.provider, cfg.api_key))
        return PNG1x1
    monkeypatch.setattr(image_gen, "generate_image_with", fake_gen)
    await image_gen._generate_image_bytes("a cat")
    assert calls == [("hf", "envtoken")]  # keyless active skipped, env last resort used
```

- [ ] **Step 2:** Run → FAIL (`generate_image_with`, `ImageGenUnavailable` missing).
- [ ] **Step 3: Implement in `app/image_gen.py`** (read the file first; insert after `generate_with_huggingface`, keep that function untouched):

```python
import uuid  # add to imports
from app import manager_client  # add to imports
from app.stt_providers import ProviderConfig  # add to imports

RUNWARE_URL = "https://api.runware.ai/v1"


class ImageGenUnavailable(Exception):
    """Provider failure that should advance the image fallback chain."""


async def _gen_hf(cfg, prompt, width=None, height=None, client=None):
    model = cfg.model or ""
    url = model if model.startswith("http") else (
        f"https://router.huggingface.co/hf-inference/models/{model}" if model
        else config.HF_MODEL_URL)
    payload = {"inputs": prompt}
    if width and height:
        payload["parameters"] = {"width": width, "height": height}
    resp = await client.post(url, headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json=payload)
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"hf HTTP {resp.status_code}")
    return resp.content


async def _gen_runware(cfg, prompt, width=None, height=None, client=None):
    task = {
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "model": cfg.model or "runware:400@4",
        "positivePrompt": prompt,
        "width": width or 512,
        "height": height or 512,
        "steps": 4,
        "numberResults": 1,
        "outputType": "base64Data",
        "outputFormat": "PNG",
        "deliveryMethod": "sync",
    }
    resp = await client.post(RUNWARE_URL,
                             headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json=[task])
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"runware HTTP {resp.status_code}")
    data = (resp.json().get("data") or [])
    if not data or not data[0].get("imageBase64Data"):
        raise ImageGenUnavailable(f"runware: no image in response ({resp.text[:200]})")
    return base64.b64decode(data[0]["imageBase64Data"])


async def _gen_fal(cfg, prompt, width=None, height=None, client=None):
    path = cfg.model or "fal-ai/flux/schnell"
    body = {"prompt": prompt}
    if width and height:
        body["image_size"] = {"width": width, "height": height}
    resp = await client.post(f"https://fal.run/{path}",
                             headers={"Authorization": f"Key {cfg.api_key}"},
                             json=body)
    if resp.status_code // 100 != 2:
        raise ImageGenUnavailable(f"fal HTTP {resp.status_code}")
    images = resp.json().get("images") or []
    if not images or not images[0].get("url"):
        raise ImageGenUnavailable("fal: no image url in response")
    img = await client.get(images[0]["url"])
    if img.status_code // 100 != 2:
        raise ImageGenUnavailable(f"fal image download HTTP {img.status_code}")
    return img.content


IMAGE_ADAPTERS = {"hf": _gen_hf, "runware": _gen_runware, "fal": _gen_fal}


async def generate_image_with(cfg: ProviderConfig, prompt: str,
                              width=None, height=None, client=None) -> bytes:
    adapter = IMAGE_ADAPTERS.get(cfg.provider)
    if adapter is None and "_" in cfg.provider:
        base = cfg.provider.split("_", 1)[0]
        adapter = IMAGE_ADAPTERS.get(base)
        if adapter is not None:
            cfg = ProviderConfig(base, cfg.model, cfg.language, cfg.api_key)
    if adapter is None:
        raise ImageGenUnavailable(f"no adapter for image provider {cfg.provider!r}")
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=120.0)
    try:
        return await adapter(cfg, prompt, width=width, height=height, client=client)
    except ImageGenUnavailable:
        raise
    except Exception as e:  # transport errors, bad JSON shape
        raise ImageGenUnavailable(f"{cfg.provider}: {e}") from e
    finally:
        if owns:
            await client.aclose()


def _image_last_resort() -> ProviderConfig | None:
    if not config.HF_API_TOKEN:
        return None
    return ProviderConfig("hf", "", "", config.HF_API_TOKEN)  # model "" -> config.HF_MODEL_URL
```

Replace the body of `_generate_image_bytes` (keep signature + comfyui branch):

```python
async def _generate_image_bytes(prompt: str, width: int | None = None,
                                height: int | None = None) -> bytes:
    """Generate raw image bytes: local ComfyUI override, else the provider chain
    (manager-api active image provider -> env HF last resort)."""
    if config.IMAGE_BACKEND == "comfyui":
        return await comfy_client.generate_png(
            prompt, width=width or 768, height=height or 768,
            timeout_s=config.COMFYUI_TIMEOUT_S)

    chain: list[ProviderConfig] = []
    active = await manager_client.get_active_image()
    if active is not None and active.api_key:
        chain.append(active)
    last = _image_last_resort()
    if last is not None and (not chain or chain[0].provider != last.provider):
        chain.append(last)  # depth <= 2
    if not chain:
        # No manager row with a key and no env token: legacy direct HF call
        # (works for public models without auth).
        if width and height:
            return await generate_with_huggingface(prompt, width=width, height=height)
        return await generate_with_huggingface(prompt)

    last_exc: Exception | None = None
    for cfg in chain:
        try:
            return await generate_image_with(cfg, prompt, width=width, height=height)
        except ImageGenUnavailable as e:
            last_exc = e
            logger.warning("Image provider %s unavailable: %s", cfg.provider, e)
    raise RuntimeError(f"All image providers failed: {last_exc}")
```

- [ ] **Step 4:** `python -m pytest tests/test_image_providers.py tests/test_providers.py -q` → all pass (existing `test_providers.py` untouched: comfyui override test unaffected; the hf-backend test monkeypatches `generate_with_huggingface` and runs with no manager URL + no HF token in test env → empty chain → legacy branch calls it). Full suite green.
- [ ] **Step 5: Commit** — `feat(image): provider adapters (hf/runware/fal) + manager-api-driven selection`

---

### Task 5: docs + key handoff + live verification

**Files:**
- Modify: `D:\line_art\README.md` (after the Multi-provider moderation subsection), `D:\line_art\CLAUDE.md` (image_gen key-files row)

- [ ] **Step 1: README** — add subsection:

```markdown
### Multi-provider image generation

The image backend is resolved the same way: manager-api's `GET /providers/active`
returns an `image` block (table `image_providers` — providers: `hf`, `runware`,
`fal`; variant rows like `runware_schnell` route by base name). The env HF token
(`HF_API_TOKEN`) is the fixed last resort, and `IMAGE_BACKEND=comfyui` still
forces the local ComfyUI path. Generation failures fall through the chain; the
imagine path still serves `IMAGINE_FALLBACK_IMAGE` if everything fails. Switch with
`PUT /livekit/providers/active/image {"provider":"runware","model":"runware:400@4","api_key":"..."}`
(or the non-clobbering `PUT /livekit/providers/image/:id/active`).
```

CLAUDE.md: update `app/image_gen.py` row: "image provider adapters (hf/runware/fal) + chain from manager-api, env HF last resort; FLUX prompt, 384px resize, 1-bit threshold, fallback image".
- [ ] **Step 2: Commit** — `docs: multi-provider image generation selection`
- [ ] **Step 3: STOP — ask the user to fill keys** in `image_providers` (runware and/or fal; hf optional since env token covers it). Do not proceed until confirmed.
- [ ] **Step 4: Live verification** — (a) `/providers/active` shows the `image` block; (b) generate via active provider (hf env) and confirm bytes; (c) activate `runware` by id (non-clobbering route), fresh-process generate → confirm PNG bytes + log `Image provider`; (d) restore prior active; (e) run one full imagine-path generation saving output for eyeball check.

---

## Self-Review

- Coverage: DB ✓ (T1), API ✓ (T2), line_art client ✓ (T3), image_gen chain ✓ (T4), docs+gate+live ✓ (T5).
- Placeholders: none; all code complete.
- Type consistency: `ProviderConfig` reused; `generate_image_with(cfg, prompt, width, height, client=None) -> bytes`; manager block `image.{provider,model,api_key}` consistent across T2–T4; adapter dict `IMAGE_ADAPTERS`; exception `ImageGenUnavailable`.
