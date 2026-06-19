import asyncio
import logging
import time

import httpx

from app import config
from app.comfy_workflow import build_flux_workflow

logger = logging.getLogger(__name__)

CLIENT_ID = "lineart"


async def generate_png(
    prompt: str,
    *,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = 0.5,
    timeout_s: float = 600.0,
    now=time.monotonic,
    sleep=asyncio.sleep,
) -> bytes:
    """Run a FLUX workflow on the local ComfyUI server and return PNG bytes."""
    base = config.COMFYUI_BASE_URL
    graph = build_flux_workflow(prompt)

    owns_client = client is None
    if owns_client:
        # Per-request timeout is capped well below the whole-workflow budget so a
        # single slow /history or /view poll raises ReadTimeout promptly instead
        # of consuming the entire timeout_s. The overall bound is enforced by the
        # deadline loop below, not by the client's per-request timeout.
        client = httpx.AsyncClient(timeout=min(30.0, timeout_s))
    try:
        try:
            resp = await client.post(
                f"{base}/prompt", json={"prompt": graph, "client_id": CLIENT_ID}
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise RuntimeError(f"ComfyUI unavailable at {base}: {e}") from e

        deadline = now() + timeout_s
        image_info = None
        while now() < deadline:
            hist = await client.get(f"{base}/history/{prompt_id}")
            hist.raise_for_status()
            entry = hist.json().get(prompt_id)
            if entry:
                for node_out in entry.get("outputs", {}).values():
                    images = node_out.get("images")
                    if images:
                        image_info = images[0]
                        break
            if image_info:
                break
            await sleep(poll_interval)

        if image_info is None:
            raise RuntimeError(f"ComfyUI timed out after {timeout_s}s waiting for image")

        view = await client.get(
            f"{base}/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": image_info.get("type", "output"),
            },
        )
        view.raise_for_status()
        return view.content
    finally:
        if owns_client:
            await client.aclose()
