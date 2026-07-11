# tests/test_stt_fallback.py
import pytest
from app import stt, config
from app.stt_providers import ProviderConfig, STTHardFailure


def test_last_resort_carries_configured_language(monkeypatch):
    """The env last-resort provider must pin STT_LANGUAGE, not auto-detect."""
    monkeypatch.setattr(config, "STT_LANGUAGE", "en")
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    assert stt._last_resort_config().language == "en"


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
async def test_empty_text_with_no_other_provider_returns_empty(monkeypatch):
    """Sole provider returns empty => "" (genuine no-speech). Nothing left to try."""
    primary = ProviderConfig("groq", "m", "", "gk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "   "
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == ""
    assert calls == ["groq"]  # chain depth 1: no second provider exists


@pytest.mark.asyncio
async def test_empty_text_falls_back_to_next_provider(monkeypatch):
    """Deepgram returns empty on ~1/3 of real device clips while Groq transcribes
    them fine. An empty active result must advance the chain, not end it."""
    primary = ProviderConfig("deepgram", "nova-2", "en", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return "" if cfg.provider == "deepgram" else "a dinosaur"
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == "a dinosaur"
    assert calls == ["deepgram", "groq"]


@pytest.mark.asyncio
async def test_all_providers_empty_returns_empty(monkeypatch):
    """Every provider heard nothing => real silence. Return "", don't raise."""
    primary = ProviderConfig("deepgram", "nova-2", "en", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    calls = []
    async def fake_tw(cfg, audio, client=None):
        calls.append(cfg.provider)
        return ""
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == ""
    assert calls == ["deepgram", "groq"]


@pytest.mark.asyncio
async def test_empty_then_hard_failure_returns_empty_not_raise(monkeypatch):
    """Active answered (empty), fallback then died: that's no-speech, not an outage."""
    primary = ProviderConfig("deepgram", "nova-2", "en", "dk")
    monkeypatch.setattr(stt.manager_client, "get_active_stt",
                        lambda client=None: _async(primary))
    monkeypatch.setattr(config, "STT_LAST_RESORT_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "gk")
    async def fake_tw(cfg, audio, client=None):
        if cfg.provider == "deepgram":
            return ""
        raise STTHardFailure("503")
    monkeypatch.setattr(stt.stt_providers, "transcribe_with", fake_tw)
    assert await stt.transcribe(b"wav") == ""


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
