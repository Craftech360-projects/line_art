# app/stt.py
import logging

from app import config
from app import manager_client
from app import stt_providers

logger = logging.getLogger(__name__)


def _last_resort_config() -> stt_providers.ProviderConfig:
    p = config.STT_LAST_RESORT_PROVIDER
    lang = config.STT_LANGUAGE
    if p == "groq":
        return stt_providers.ProviderConfig("groq", config.GROQ_MODEL, lang, config.GROQ_API_KEY)
    if p == "deepgram":
        return stt_providers.ProviderConfig("deepgram", "nova-2", lang, config.DEEPGRAM_API_KEY)
    if p == "sarvam":
        return stt_providers.ProviderConfig("sarvam", config.SARVAM_MODEL, lang, config.SARVAM_API_KEY)
    if p == "speaches":
        # api_key field carries the Speaches base URL for the local dev path.
        return stt_providers.ProviderConfig("speaches", config.SPEACHES_MODEL, lang, config.SPEACHES_BASE_URL)
    return stt_providers.ProviderConfig("groq", config.GROQ_MODEL, lang, config.GROQ_API_KEY)


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
    """Transcribe audio to text via the active provider, falling back to the env
    last-resort on a hard failure OR an empty transcription. Some providers answer
    200-with-no-text on speech another provider hears fine, so an empty result
    advances the chain. Empty from every provider is real silence: return "".
    """
    chain = await _resolve_chain(client)
    last_exc: Exception | None = None
    answered = False
    for cfg in chain:
        try:
            text = (await stt_providers.transcribe_with(cfg, audio_bytes, client)).strip()
        except stt_providers.STTHardFailure as e:
            last_exc = e
            logger.warning("STT provider %s hard-failed: %s", cfg.provider, e)
            continue
        answered = True
        if text:
            return text
        logger.info("STT provider %s returned empty; trying next provider", cfg.provider)
    if answered:
        return ""
    raise RuntimeError(f"All STT providers failed: {last_exc}")
