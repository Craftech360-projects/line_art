import pytest

from app import main


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_handle_text_input_emits_progress_then_result(monkeypatch):
    async def fake_generate(subject):
        return ("data:image/png;base64,AAA", f"prompt for {subject}", "cm9hd19tb25v", 8)

    monkeypatch.setattr(main, "generate_line_art", fake_generate)
    ws = FakeWS()
    await main.handle_text_input(ws, "a cat")

    joined = " ".join(ws.sent)
    assert "progress" in joined
    assert "result" in joined
    assert "prompt for a cat" in joined


@pytest.mark.asyncio
async def test_handle_text_input_empty_sends_error():
    ws = FakeWS()
    await main.handle_text_input(ws, "   ")
    assert "error" in ws.sent[0]
    assert "Empty" in ws.sent[0]


@pytest.mark.asyncio
async def test_handle_text_input_reports_service_error(monkeypatch):
    async def boom(subject):
        raise RuntimeError("ComfyUI unavailable at http://localhost:8188")

    monkeypatch.setattr(main, "generate_line_art", boom)
    ws = FakeWS()
    await main.handle_text_input(ws, "a cat")
    assert "error" in ws.sent[-1]
    assert "ComfyUI unavailable" in ws.sent[-1]
