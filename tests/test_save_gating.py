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
