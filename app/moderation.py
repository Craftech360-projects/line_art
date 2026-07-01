"""LLM child-safety moderation for AI Imagine prompts.

A second layer on top of the keyword filter in image_gen._assert_child_safe. An LLM
classifier is multilingual and catches obfuscation / phrasing the keyword list misses.
Reuses the existing Groq API key (same account used for STT).
"""
import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM = (
    "You are a strict content-safety filter for an image generator used by children "
    "aged 3 to 8. Decide whether the requested image subject is appropriate. "
    "UNSAFE = violence, weapons, gore, death, horror or scary content, sexual or "
    "romantic themes, nudity, drugs, alcohol, tobacco, hate or extremism, self-harm, "
    "or any other adult or frightening topic. Everything wholesome and child-friendly "
    "is SAFE. The request may be in any language. Reply with exactly one word: "
    "SAFE or UNSAFE."
)


async def is_prompt_safe(subject: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Return (True, "") if the subject is child-safe, else (False, reason).

    Fails OPEN: if no Groq key is configured (dev/test) or the API errors, returns
    (True, "") — the keyword filter in image_gen remains the backstop, so a Groq
    outage degrades rather than blocking every image.
    """
    if config.MODERATION_BACKEND == "off" or not config.GROQ_API_KEY:
        return True, ""  # disabled or no key -> skip LLM layer; keyword filter still applies

    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    payload = {
        "model": config.GROQ_LLM_MODEL,
        "temperature": 0,
        "max_tokens": 3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": subject},
        ],
    }
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(GROQ_CHAT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        verdict = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if verdict.startswith("UNSAFE"):
            logger.info("Moderation blocked subject: %r", subject)
            return False, "content not allowed for children"
        return True, ""
    except Exception as e:
        logger.warning("Moderation unavailable, failing open: %s", e)
        return True, ""
    finally:
        if owns_client:
            await client.aclose()
