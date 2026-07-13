# Moderation Provider Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make line_art's child-safety moderation provider admin-switchable (groq / openai / openrouter / openai_moderation) via a new `moderation_providers` DB table served by manager-api, mirroring the existing STT provider pattern (ADR-0002).

**Architecture:** A new `moderation_providers` table (clone of `stt_providers`, minus audio fields) lives in the Supabase Postgres used by cheeko-backend. manager-api-node's existing `/livekit/providers/*` endpoints gain a `moderation` section (same service map pattern). line_art's `manager_client` already fetches `/providers/active`; it now also parses the `moderation` block from the same response (one fetch, one cache). `app/moderation.py` grows provider adapters and a 2-deep fallback chain (active → env Groq last-resort), keeping the existing fail-open behavior and the `is_prompt_safe(subject) -> (bool, str)` signature so `image_gen.py` is untouched.

**Tech Stack:** Node/Express + Prisma (manager-api), Python/httpx/pytest (line_art), Supabase Postgres.

## Global Constraints

- Mirror the STT provider pattern exactly — same table shape, same service map, same route shape, same line_art chain/fallback semantics. No new abstractions beyond it.
- Moderation must keep **failing open** end-to-end (Groq outage ⇒ keyword filter is the backstop) and keep the public signature `is_prompt_safe(subject, client=None) -> tuple[bool, str]`.
- Provider names (DB `provider_name` values): `groq`, `openai`, `openrouter`, `openai_moderation`.
- Do not commit real API keys anywhere. Seed rows get `api_key = ''`; the user fills them in the DB afterwards.
- manager-api repo: `D:\cheeko-backend\main\manager-api-node`. line_art repo: `D:\line_art`. Each task commits in its own repo.
- DB connection for migration: use the `DIRECT_URL` the user supplied (do not print it into committed files).

---

### Task 1: `moderation_providers` table (schema + migration + seed)

**Files:**
- Create: `D:\cheeko-backend\main\manager-api-node\prisma\migrations\20260708000000_add_moderation_providers\migration.sql`
- Modify: `D:\cheeko-backend\main\manager-api-node\prisma\schema.prisma` (append after `model stt_providers` block, ~line 1227)

**Interfaces:**
- Produces: Prisma delegate `prisma.moderation_providers` with columns `id BigInt, provider_name String @unique, api_key String, model String, is_active Boolean, priority Int, config_json Json?, created_at, updated_at` — Task 2 depends on these exact names.

- [ ] **Step 1: Write the migration SQL**

```sql
-- prisma/migrations/20260708000000_add_moderation_providers/migration.sql
CREATE TABLE IF NOT EXISTS "moderation_providers" (
    "id"            BIGSERIAL PRIMARY KEY,
    "provider_name" TEXT NOT NULL,
    "api_key"       TEXT NOT NULL DEFAULT '',
    "model"         TEXT NOT NULL DEFAULT '',
    "is_active"     BOOLEAN NOT NULL DEFAULT false,
    "priority"      INTEGER NOT NULL DEFAULT 0,
    "config_json"   JSONB,
    "created_at"    TIMESTAMPTZ(6) DEFAULT now(),
    "updated_at"    TIMESTAMPTZ(6) DEFAULT now(),
    CONSTRAINT "moderation_providers_provider_name_key" UNIQUE ("provider_name")
);

CREATE INDEX IF NOT EXISTS "idx_moderation_active" ON "moderation_providers" ("is_active");
CREATE INDEX IF NOT EXISTS "idx_moderation_priority" ON "moderation_providers" ("priority" DESC);

-- Seed the three chat judges + the free OpenAI classifier. Keys are filled by the
-- admin in the DB afterwards. groq starts active = current behavior preserved.
INSERT INTO "moderation_providers" ("provider_name", "model", "is_active", "priority") VALUES
    ('groq',              'llama-3.1-8b-instant',    true,  100),
    ('openai',            'gpt-4o-mini',             false, 50),
    ('openrouter',        'google/gemma-3-4b-it',    false, 40),
    ('openai_moderation', 'omni-moderation-latest',  false, 30)
ON CONFLICT ("provider_name") DO NOTHING;
```

- [ ] **Step 2: Add the Prisma model**

Append to `prisma/schema.prisma` directly after the `stt_providers` model (before `model tts_providers`):

```prisma
model moderation_providers {
  id            BigInt    @id @default(autoincrement())
  provider_name String    @unique
  api_key       String    @default("")
  model         String    @default("")
  is_active     Boolean   @default(false)
  priority      Int       @default(0)
  config_json   Json?
  created_at    DateTime? @default(now()) @db.Timestamptz(6)
  updated_at    DateTime? @default(now()) @db.Timestamptz(6)

  @@index([is_active], map: "idx_moderation_active")
  @@index([priority(sort: Desc)], map: "idx_moderation_priority")
}
```

- [ ] **Step 3: Apply the migration to Supabase**

From `D:\cheeko-backend\main\manager-api-node` (PowerShell; paste the user-supplied DIRECT_URL as the env var value — do not commit it):

```powershell
$env:DIRECT_DATABASE_URL = "<DIRECT_URL from user>"
npx prisma db execute --url "$env:DIRECT_DATABASE_URL" --file "prisma\migrations\20260708000000_add_moderation_providers\migration.sql"
```

Expected: exits 0, no error output.

- [ ] **Step 4: Regenerate the Prisma client**

```powershell
npx prisma generate
```

Expected: `Generated Prisma Client` message.

- [ ] **Step 5: Verify the table + seed rows exist**

```powershell
'SELECT provider_name, model, is_active, priority FROM moderation_providers ORDER BY priority DESC;' | Out-File -Encoding utf8 check.sql
npx prisma db execute --url "$env:DIRECT_DATABASE_URL" --file check.sql
Remove-Item check.sql
```

Expected: exits 0 (db execute doesn't print rows; success = table exists and SQL is valid). Alternatively verify via any SQL client: 4 rows, `groq` active.

- [ ] **Step 6: Commit (manager-api repo)**

```powershell
git -C D:\cheeko-backend\main\manager-api-node add prisma/schema.prisma prisma/migrations/20260708000000_add_moderation_providers/migration.sql
git -C D:\cheeko-backend\main\manager-api-node commit -m "feat(providers): add moderation_providers table (groq/openai/openrouter/openai_moderation)"
```

---

### Task 2: manager-api — serve + manage moderation providers

**Files:**
- Modify: `D:\cheeko-backend\main\manager-api-node\src\services\livekitProviders.service.js`
- Modify: `D:\cheeko-backend\main\manager-api-node\src\routes\livekitProviders.routes.js`
- Test: `D:\cheeko-backend\main\manager-api-node\tests\unit\livekit-providers.moderation.test.js`

**Interfaces:**
- Consumes: `prisma.moderation_providers` delegate from Task 1.
- Produces: `GET /livekit/providers/active` response gains `moderation: { provider, model, api_key } | null`. `PUT /livekit/providers/active/moderation` accepts `{ provider, model?, api_key?, priority? }`. Generic `PUT /providers/:type/:id` and `PUT /providers/:type/:id/active` accept `type = "moderation"`. Task 3 (line_art) parses the `moderation` block.

- [ ] **Step 1: Write the failing unit test**

```javascript
// tests/unit/livekit-providers.moderation.test.js
const mockPrisma = {
  llm_providers: { findFirst: jest.fn().mockResolvedValue(null), findMany: jest.fn().mockResolvedValue([]) },
  stt_providers: { findFirst: jest.fn().mockResolvedValue(null), findMany: jest.fn().mockResolvedValue([]) },
  tts_providers: { findFirst: jest.fn().mockResolvedValue(null), findMany: jest.fn().mockResolvedValue([]) },
  moderation_providers: {
    findFirst: jest.fn(),
    findMany: jest.fn().mockResolvedValue([]),
    updateMany: jest.fn(),
    upsert: jest.fn()
  },
  $transaction: jest.fn(async (fn) => fn(mockPrisma))
};

jest.mock('../../src/config/database', () => ({ prisma: mockPrisma }));

const service = require('../../src/services/livekitProviders.service');

describe('moderation providers', () => {
  beforeEach(() => jest.clearAllMocks());

  test('getActiveProviders includes the active moderation provider', async () => {
    mockPrisma.moderation_providers.findFirst.mockResolvedValue({
      id: 1n, provider_name: 'groq', model: 'llama-3.1-8b-instant',
      api_key: 'gk', is_active: true, priority: 100, updated_at: new Date()
    });
    const out = await service.getActiveProviders();
    expect(out.moderation).toEqual({
      provider: 'groq', model: 'llama-3.1-8b-instant', api_key: 'gk'
    });
  });

  test('getActiveProviders returns moderation null when none active', async () => {
    mockPrisma.moderation_providers.findFirst.mockResolvedValue(null);
    const out = await service.getActiveProviders();
    expect(out.moderation).toBeNull();
  });

  test('setActiveModerationProvider deactivates others and upserts', async () => {
    mockPrisma.moderation_providers.upsert.mockResolvedValue({ id: 2n, provider_name: 'openai' });
    await service.setActiveModerationProvider({ provider: 'openai', model: 'gpt-4o-mini', api_key: 'sk' });
    expect(mockPrisma.moderation_providers.updateMany).toHaveBeenCalledWith(
      expect.objectContaining({ where: { is_active: true } }));
    expect(mockPrisma.moderation_providers.upsert).toHaveBeenCalledWith(
      expect.objectContaining({ where: { provider_name: 'openai' } }));
  });

  test('updateProvider accepts type "moderation"', async () => {
    mockPrisma.moderation_providers.update = jest.fn().mockResolvedValue({ id: 1n, provider_name: 'groq' });
    await service.updateProvider('moderation', '1', { api_key: 'newkey' });
    expect(mockPrisma.moderation_providers.update).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```powershell
cd D:\cheeko-backend\main\manager-api-node; npx jest tests/unit/livekit-providers.moderation.test.js
```

Expected: FAIL — `out.moderation` is `undefined`, `setActiveModerationProvider` is not a function.

- [ ] **Step 3: Implement in livekitProviders.service.js**

3a. Add a `moderation` entry to the `providerModels` map (after the `tts` entry, ~line 77):

```javascript
  moderation: {
    delegate: 'moderation_providers',
    updateFields: {
      provider_name: 'string',
      model: 'string',
      api_key: 'string',
      priority: 'int',
      config_json: 'json'
    }
  }
```

3b. In `listProviders` change the parallel fetch to include moderation:

```javascript
const listProviders = async () => {
  const orderBy = [{ is_active: 'desc' }, { priority: 'desc' }, { updated_at: 'desc' }];
  const [llm, stt, tts, moderation] = await Promise.all([
    prisma.llm_providers.findMany({ orderBy }),
    prisma.stt_providers.findMany({ orderBy }),
    prisma.tts_providers.findMany({ orderBy }),
    prisma.moderation_providers.findMany({ orderBy })
  ]);

  return {
    llm: (llm || []).map(normalizeProviderRow),
    stt: (stt || []).map(normalizeProviderRow),
    tts: (tts || []).map(normalizeProviderRow),
    moderation: (moderation || []).map(normalizeProviderRow)
  };
};
```

3c. In `getActiveProviders` add the moderation fetch and response block:

```javascript
const getActiveProviders = async () => {
  const [llm, stt, tts, moderation] = await Promise.all([
    prisma.llm_providers.findFirst({
      where: { is_active: true },
      orderBy: [{ priority: 'desc' }, { updated_at: 'desc' }]
    }),
    prisma.stt_providers.findFirst({
      where: { is_active: true },
      orderBy: [{ priority: 'desc' }, { updated_at: 'desc' }]
    }),
    prisma.tts_providers.findFirst({
      where: { is_active: true },
      orderBy: [{ priority: 'desc' }, { updated_at: 'desc' }]
    }),
    prisma.moderation_providers.findFirst({
      where: { is_active: true },
      orderBy: [{ priority: 'desc' }, { updated_at: 'desc' }]
    })
  ]);

  return {
    updated_at: pickLatestUpdatedAt([llm, stt, tts, moderation]),
    llm: llm ? { /* unchanged existing block */ } : null,
    stt: stt ? { /* unchanged existing block */ } : null,
    tts: tts ? { /* unchanged existing block */ } : null,
    moderation: moderation ? {
      provider: moderation.provider_name,
      model: moderation.model || '',
      api_key: moderation.api_key || ''
    } : null
  };
};
```

(Keep the existing llm/stt/tts object bodies exactly as they are — only the destructuring line, the `pickLatestUpdatedAt` array, and the added `moderation` key change.)

3d. Add `setActiveModerationProvider` after `setActiveTTSProvider` (mirror of `setActiveSTTProvider`, no `language`):

```javascript
const setActiveModerationProvider = async (payload = {}) => {
  const providerName = toRequiredString(payload.provider, 'provider');
  const model = toNullableString(payload.model) || '';
  const apiKey = toNullableString(payload.api_key) || '';
  const priority = toOptionalInt(payload.priority, 'priority') ?? 0;

  const updated = await prisma.$transaction(async (tx) => {
    await tx.moderation_providers.updateMany({
      where: { is_active: true },
      data: { is_active: false, updated_at: new Date() }
    });

    return tx.moderation_providers.upsert({
      where: { provider_name: providerName },
      create: {
        provider_name: providerName,
        model,
        api_key: apiKey,
        is_active: true,
        priority
      },
      update: {
        model,
        api_key: apiKey,
        is_active: true,
        priority,
        updated_at: new Date()
      }
    });
  });

  return updated;
};
```

3e. Export it: add `setActiveModerationProvider` to `module.exports`.

- [ ] **Step 4: Add the route**

In `src/routes/livekitProviders.routes.js`, after the `PUT /providers/active/tts` route (~line 62):

```javascript
router.put('/providers/active/moderation',
  requireAdmin,
  asyncHandler(async (req, res) => {
    try {
      await livekitProvidersService.setActiveModerationProvider(req.body || {});
      const data = await livekitProvidersService.getActiveProviders();
      success(res, data, 'Moderation provider updated');
    } catch (error) {
      badRequest(res, error.message);
    }
  })
);
```

(The generic `PUT /providers/:type/:id` and `PUT /providers/:type/:id/active` routes need no change — the `providerModels` map entry makes `type = "moderation"` work.)

- [ ] **Step 5: Run the test to verify it passes**

```powershell
npx jest tests/unit/livekit-providers.moderation.test.js
```

Expected: PASS (4 tests).

- [ ] **Step 6: Run the full jest suite to check for regressions**

```powershell
npm test
```

Expected: same pass/fail state as before this change (no new failures).

- [ ] **Step 7: Commit (manager-api repo)**

```powershell
git -C D:\cheeko-backend\main\manager-api-node add src/services/livekitProviders.service.js src/routes/livekitProviders.routes.js tests/unit/livekit-providers.moderation.test.js
git -C D:\cheeko-backend\main\manager-api-node commit -m "feat(providers): moderation provider in active-providers API + PUT /providers/active/moderation"
```

---

### Task 3: line_art — parse `moderation` from `/providers/active`

**Files:**
- Modify: `D:\line_art\app\manager_client.py` (whole-file rewrite below — it is 61 lines)
- Test: `D:\line_art\tests\test_manager_client.py` (extend; create if absent)

**Interfaces:**
- Consumes: `moderation: {provider, model, api_key}` block from Task 2.
- Produces: `async get_active_moderation(client=None, now=None) -> ProviderConfig | None` and (unchanged behavior) `async get_active_stt(client=None, now=None) -> ProviderConfig | None`. Both share one fetch + one TTL cache. `ProviderConfig` is the existing dataclass from `app/stt_providers.py` (`provider, model, language, api_key`); moderation configs carry `language=""`.

- [ ] **Step 1: Write the failing tests**

Append to `D:\line_art\tests\test_manager_client.py` (create the file with this content if it doesn't exist):

```python
"""manager_client: moderation block parsing + shared cache."""
import httpx
import pytest

from app import config, manager_client


def _client(payload: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _manager_env(monkeypatch):
    monkeypatch.setattr(config, "MANAGER_API_BASE_URL", "http://mgr")
    monkeypatch.setattr(config, "STT_PROVIDER_TTL_S", 300.0)
    manager_client._cache.update({"data": None, "ts": 0.0})


@pytest.mark.asyncio
async def test_moderation_block_is_parsed():
    payload = {"data": {
        "stt": {"provider": "groq", "model": "whisper-large-v3", "api_key": "sk1"},
        "moderation": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk2"},
    }}
    async with _client(payload) as c:
        cfg = await manager_client.get_active_moderation(client=c, now=1000.0)
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk2"


@pytest.mark.asyncio
async def test_missing_moderation_block_returns_none():
    payload = {"data": {"stt": {"provider": "groq", "api_key": "sk1"}}}
    async with _client(payload) as c:
        assert await manager_client.get_active_moderation(client=c, now=1000.0) is None


@pytest.mark.asyncio
async def test_stt_and_moderation_share_one_fetch_and_cache():
    calls = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {
            "stt": {"provider": "groq", "api_key": "sk1"},
            "moderation": {"provider": "groq", "api_key": "sk1"},
        }})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await manager_client.get_active_stt(client=c, now=1000.0)
        await manager_client.get_active_moderation(client=c, now=1001.0)  # within TTL
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_failure_serves_last_known_good_moderation():
    ok = {"data": {"moderation": {"provider": "groq", "api_key": "sk1"}}}
    state = {"fail": False}
    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=ok)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        first = await manager_client.get_active_moderation(client=c, now=1000.0)
        state["fail"] = True
        second = await manager_client.get_active_moderation(client=c, now=2000.0)  # past TTL
    assert first.provider == "groq"
    assert second is not None and second.provider == "groq"  # last-known-good
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
cd D:\line_art; python -m pytest tests/test_manager_client.py -q
```

Expected: FAIL — `AttributeError: module 'app.manager_client' has no attribute 'get_active_moderation'` (and `_cache` has no `data` key).

- [ ] **Step 3: Rewrite `app/manager_client.py`**

```python
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
```

- [ ] **Step 4: Run the new tests + the whole suite**

```powershell
python -m pytest tests/test_manager_client.py -q
python -m pytest -q
```

Expected: new tests PASS; full suite has no new failures. If any existing test pokes `manager_client._cache["cfg"]` directly, update it to the new `{"data", "ts"}` shape.

- [ ] **Step 5: Commit (line_art repo)**

```powershell
git -C D:\line_art add app/manager_client.py tests/test_manager_client.py
git -C D:\line_art commit -m "feat(moderation): fetch active moderation provider from manager-api (shared cache)"
```

---

### Task 4: line_art — moderation provider adapters + fallback chain

**Files:**
- Modify: `D:\line_art\app\moderation.py` (whole-file rewrite below — it is 65 lines)
- Test: `D:\line_art\tests\test_moderation_providers.py` (new)

**Interfaces:**
- Consumes: `manager_client.get_active_moderation()` from Task 3; `ProviderConfig` from `app/stt_providers.py`; `config.GROQ_API_KEY`, `config.GROQ_LLM_MODEL`, `config.MODERATION_BACKEND` (all existing — no config.py change needed).
- Produces: unchanged public API `async is_prompt_safe(subject, client=None) -> tuple[bool, str]` (so `image_gen.py` and existing tests keep working). Internal: `ADAPTERS` dict keyed by provider name; `ModerationUnavailable` exception drives the fallback chain.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_moderation_providers.py
"""Moderation provider adapters + fallback chain."""
import httpx
import pytest

from app import config, moderation
from app.stt_providers import ProviderConfig


def _chat_ok(verdict: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": verdict}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "MODERATION_BACKEND", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    monkeypatch.setattr(config, "GROQ_LLM_MODEL", "llama-3.1-8b-instant")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,url_host", [
    ("groq", "api.groq.com"),
    ("openai", "api.openai.com"),
    ("openrouter", "openrouter.ai"),
])
async def test_chat_adapter_hits_right_host_and_parses_safe(provider, url_host):
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "SAFE"}}]})
    cfg = ProviderConfig(provider, "some-model", "", "key123")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.check_with(cfg, "a happy puppy", c)
    assert safe is True and reason == ""
    assert seen["host"] == url_host
    assert seen["auth"] == "Bearer key123"


@pytest.mark.asyncio
async def test_chat_adapter_unsafe_verdict_blocks():
    cfg = ProviderConfig("groq", "llama-3.1-8b-instant", "", "gk")
    async with _chat_ok("UNSAFE") as c:
        safe, reason = await moderation.check_with(cfg, "something bad", c)
    assert safe is False and reason


@pytest.mark.asyncio
async def test_openai_moderation_adapter_parses_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/moderations"
        return httpx.Response(200, json={"results": [{"flagged": True}]})
    cfg = ProviderConfig("openai_moderation", "omni-moderation-latest", "", "sk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.check_with(cfg, "something", c)
    assert safe is False


@pytest.mark.asyncio
async def test_http_error_raises_moderation_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    cfg = ProviderConfig("groq", "llama-3.1-8b-instant", "", "gk")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(moderation.ModerationUnavailable):
            await moderation.check_with(cfg, "anything", c)


@pytest.mark.asyncio
async def test_chain_falls_back_to_last_resort_on_active_failure(monkeypatch):
    async def active_openai(client=None, now=None):
        return ProviderConfig("openai", "gpt-4o-mini", "", "sk-bad")
    monkeypatch.setattr(moderation.manager_client, "get_active_moderation", active_openai)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            return httpx.Response(500)  # active provider hard-fails
        return httpx.Response(200, json={"choices": [{"message": {"content": "UNSAFE"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.is_prompt_safe("something", client=c)
    assert safe is False  # groq last-resort answered, not fail-open


@pytest.mark.asyncio
async def test_all_providers_down_fails_open(monkeypatch):
    async def no_active(client=None, now=None):
        return None
    monkeypatch.setattr(moderation.manager_client, "get_active_moderation", no_active)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        safe, reason = await moderation.is_prompt_safe("anything", client=c)
    assert safe is True  # fail-open preserved


@pytest.mark.asyncio
async def test_moderation_off_skips_everything(monkeypatch):
    monkeypatch.setattr(config, "MODERATION_BACKEND", "off")
    safe, _ = await moderation.is_prompt_safe("anything")
    assert safe is True
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
python -m pytest tests/test_moderation_providers.py -q
```

Expected: FAIL — `moderation` has no attribute `check_with` / `ModerationUnavailable` / `manager_client`.

- [ ] **Step 3: Rewrite `app/moderation.py`**

```python
"""LLM child-safety moderation for AI Imagine prompts.

A second layer on top of the keyword filter in image_gen._assert_child_safe.
The active provider comes from manager-api (moderation_providers table); the
env-configured Groq setup is the fixed last resort. All providers share the
same verdict contract; the whole layer FAILS OPEN (keyword filter remains the
backstop) so a provider outage degrades rather than blocking every image.
"""
import logging

import httpx

from app import config
from app import manager_client
from app.stt_providers import ProviderConfig

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a strict content-safety filter for an image generator used by children "
    "aged 3 to 8. Decide whether the requested image subject is appropriate. "
    "UNSAFE = violence, weapons, gore, death, horror or scary content, sexual or "
    "romantic themes, nudity, drugs, alcohol, tobacco, hate or extremism, self-harm, "
    "or any other adult or frightening topic. Everything wholesome and child-friendly "
    "is SAFE. The request may be in any language. Reply with exactly one word: "
    "SAFE or UNSAFE."
)

_CHAT_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
_OPENAI_MODERATION_URL = "https://api.openai.com/v1/moderations"

_BLOCK_REASON = "content not allowed for children"


class ModerationUnavailable(Exception):
    """Provider failure that should advance the fallback chain."""


async def _chat(cfg: ProviderConfig, subject: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "max_tokens": 3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": subject},
        ],
    }
    resp = await client.post(_CHAT_URLS[cfg.provider],
                             headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json=payload)
    if resp.status_code // 100 != 2:
        raise ModerationUnavailable(f"{cfg.provider} HTTP {resp.status_code}")
    verdict = resp.json()["choices"][0]["message"]["content"].strip().upper()
    if verdict.startswith("UNSAFE"):
        return False, _BLOCK_REASON
    return True, ""


async def _openai_moderation(cfg: ProviderConfig, subject: str,
                             client: httpx.AsyncClient) -> tuple[bool, str]:
    resp = await client.post(_OPENAI_MODERATION_URL,
                             headers={"Authorization": f"Bearer {cfg.api_key}"},
                             json={"model": cfg.model or "omni-moderation-latest",
                                   "input": subject})
    if resp.status_code // 100 != 2:
        raise ModerationUnavailable(f"openai_moderation HTTP {resp.status_code}")
    flagged = bool(resp.json()["results"][0]["flagged"])
    return (False, _BLOCK_REASON) if flagged else (True, "")


ADAPTERS = {
    "groq": _chat,
    "openai": _chat,
    "openrouter": _chat,
    "openai_moderation": _openai_moderation,
}


async def check_with(cfg: ProviderConfig, subject: str,
                     client: httpx.AsyncClient) -> tuple[bool, str]:
    adapter = ADAPTERS.get(cfg.provider)
    if adapter is None:
        raise ModerationUnavailable(f"no adapter for provider {cfg.provider!r}")
    try:
        return await adapter(cfg, subject, client)
    except ModerationUnavailable:
        raise
    except Exception as e:  # transport errors, bad JSON shape
        raise ModerationUnavailable(f"{cfg.provider}: {e}") from e


def _last_resort() -> ProviderConfig | None:
    if not config.GROQ_API_KEY:
        return None
    return ProviderConfig("groq", config.GROQ_LLM_MODEL, "", config.GROQ_API_KEY)


async def is_prompt_safe(subject: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Return (True, "") if the subject is child-safe, else (False, reason).

    Chain: manager-api active provider -> env Groq last resort. FAILS OPEN if
    every provider is unavailable (keyword filter remains the backstop)."""
    if config.MODERATION_BACKEND == "off":
        return True, ""

    chain: list[ProviderConfig] = []
    active = await manager_client.get_active_moderation(client=client)
    if active is not None and active.api_key:
        chain.append(active)
    last = _last_resort()
    if last is not None and (not chain or chain[0].provider != last.provider):
        chain.append(last)  # depth <= 2
    if not chain:
        return True, ""  # no configured provider (dev/test) -> keyword filter only

    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        for cfg in chain:
            try:
                verdict = await check_with(cfg, subject, client)
                if not verdict[0]:
                    logger.info("Moderation[%s] blocked subject: %r", cfg.provider, subject)
                return verdict
            except ModerationUnavailable as e:
                logger.warning("Moderation provider %s unavailable: %s", cfg.provider, e)
        logger.warning("All moderation providers unavailable; failing open")
        return True, ""
    finally:
        if owns:
            await client.aclose()
```

- [ ] **Step 4: Run the new tests + full suite**

```powershell
python -m pytest tests/test_moderation_providers.py -q
python -m pytest -q
```

Expected: new tests PASS. Existing `tests/test_imagine_safety.py::test_moderation_fails_open_without_key` must still pass (no manager URL + no Groq key ⇒ empty chain ⇒ fail open). No other failures.

- [ ] **Step 5: Commit (line_art repo)**

```powershell
git -C D:\line_art add app/moderation.py tests/test_moderation_providers.py
git -C D:\line_art commit -m "feat(moderation): provider adapters (groq/openai/openrouter/openai_moderation) + manager-api-driven selection"
```

---

### Task 5: Docs + key-update handoff + live verification

**Files:**
- Modify: `D:\line_art\README.md` (Configuration section, after the "Multi-provider STT" subsection ~line 138)
- Modify: `D:\line_art\CLAUDE.md` (project section — moderation line)

**Interfaces:**
- Consumes: everything above, running end-to-end.

- [ ] **Step 1: Document the moderation provider selection**

Add to `README.md` after the Multi-provider STT subsection:

```markdown
### Multi-provider moderation

The child-safety moderation provider is resolved the same way as STT: manager-api's
`GET /providers/active` now returns a `moderation` block (backed by the
`moderation_providers` table — providers: `groq`, `openai`, `openrouter`,
`openai_moderation`). The env-configured Groq judge (`GROQ_API_KEY` +
`GROQ_LLM_MODEL`) is the fixed last resort, and the whole layer still fails open
to the keyword filter if every provider is down. Switch the active provider with
`PUT /livekit/providers/active/moderation {"provider": "openai", "model": "gpt-4o-mini", "api_key": "..."}`.
```

Update the moderation line in `CLAUDE.md`'s key-files table: `app/moderation.py` → "pluggable moderation providers (groq/openai/openrouter/openai_moderation), active one from manager-api, Groq env last resort, fails open".

- [ ] **Step 2: Commit docs (line_art repo)**

```powershell
git -C D:\line_art add README.md CLAUDE.md
git -C D:\line_art commit -m "docs: multi-provider moderation selection"
```

- [ ] **Step 3: STOP — ask the user to fill API keys in the DB**

Tell the user (verbatim requirement from the spec): the `moderation_providers` table is seeded with 4 rows and empty `api_key`s. Ask them to update keys, e.g. in Supabase SQL editor:

```sql
UPDATE moderation_providers SET api_key = '<GROQ KEY>'       WHERE provider_name = 'groq';
UPDATE moderation_providers SET api_key = '<OPENAI KEY>'     WHERE provider_name = 'openai';
UPDATE moderation_providers SET api_key = '<OPENROUTER KEY>' WHERE provider_name = 'openrouter';
UPDATE moderation_providers SET api_key = '<OPENAI KEY>'     WHERE provider_name = 'openai_moderation';
```

Do not proceed to Step 4 until the user confirms.

- [ ] **Step 4: Live verification (after user confirms keys)**

4a. Start manager-api (`npm run dev` in the manager-api repo, or use the deployed instance) and verify the moderation block:

```powershell
curl.exe -s -H "X-Service-Key: <SERVICE_SECRET_KEY>" "<MANAGER_API_BASE_URL>/providers/active"
```

Expected: JSON containing `"moderation": {"provider": "groq", "model": "llama-3.1-8b-instant", "api_key": "..."}`.

4b. Verify line_art resolves and uses it (from `D:\line_art`, with `MANAGER_API_BASE_URL` + `SERVICE_SECRET_KEY` set in `.env`):

```powershell
python -c "import asyncio; from app import moderation; print(asyncio.run(moderation.is_prompt_safe('a happy puppy')))"
python -c "import asyncio; from app import moderation; print(asyncio.run(moderation.is_prompt_safe('a bloody zombie with a gun')))"
```

Expected: `(True, '')` then `(False, 'content not allowed for children')`.

4c. Switch the active provider and re-run 4b to prove the switch takes effect (restart line_art or wait out the 300 s TTL):

```powershell
curl.exe -s -X PUT -H "X-Service-Key: <SERVICE_SECRET_KEY>" -H "Content-Type: application/json" -d "{\"provider\":\"openai\",\"model\":\"gpt-4o-mini\"}" "<MANAGER_API_BASE_URL>/providers/active/moderation"
```

Expected: same SAFE/UNSAFE verdicts via the OpenAI provider (visible in line_art logs as `Moderation[openai] ...`).

---

## Self-Review

- **Spec coverage:** new DB table ✓ (Task 1), provider API updated to serve it ✓ (Task 2), line_art fetches + uses it ✓ (Tasks 3–4), stop-for-keys-then-test flow ✓ (Task 5 Steps 3–4).
- **Placeholder scan:** all steps carry complete code/commands. The one intentional elision (Task 2 Step 3c "unchanged existing block") refers to code that must NOT change and is shown in full in the current file.
- **Type consistency:** `ProviderConfig(provider, model, language, api_key)` reused everywhere; `check_with(cfg, subject, client) -> tuple[bool, str]`; `get_active_moderation(client=None, now=None) -> ProviderConfig | None`; manager-api response key `moderation.{provider,model,api_key}` consistent across Tasks 2–4.
