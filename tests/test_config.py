import importlib
import os


def _reload_config():
    import app.config
    return importlib.reload(app.config)


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("SPEACHES_BASE_URL", raising=False)
    monkeypatch.delenv("SPEACHES_MODEL", raising=False)
    monkeypatch.delenv("COMFYUI_BASE_URL", raising=False)
    cfg = _reload_config()
    assert cfg.SPEACHES_BASE_URL == "http://localhost:8001"
    assert cfg.SPEACHES_MODEL == "Systran/faster-whisper-large-v3"
    assert cfg.COMFYUI_BASE_URL == "http://localhost:8188"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SPEACHES_BASE_URL", "http://host:9000")
    monkeypatch.setenv("COMFYUI_BASE_URL", "http://host:9188")
    cfg = _reload_config()
    assert cfg.SPEACHES_BASE_URL == "http://host:9000"
    assert cfg.COMFYUI_BASE_URL == "http://host:9188"
