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
_DEFAULT_CHAT_MODELS = {
    "groq": "llama-3.1-8b-instant",
    "openai": "gpt-4o-mini",
    "openrouter": "google/gemma-3-4b-it",
}
_OPENAI_MODERATION_URL = "https://api.openai.com/v1/moderations"

_BLOCK_REASON = "content not allowed for children"


class ModerationUnavailable(Exception):
    """Provider failure that should advance the fallback chain."""


async def _chat(cfg: ProviderConfig, subject: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    payload = {
        "model": cfg.model or _DEFAULT_CHAT_MODELS[cfg.provider],
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
