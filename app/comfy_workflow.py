"""Builds a ComfyUI prompt-graph for FLUX.1-schnell fp8.

The graph uses the single-file fp8 checkpoint (CheckpointLoaderSimple), which
bundles UNet + CLIP + VAE, so no separate loaders are needed.
"""

CKPT_NAME = "flux1-schnell-fp8.safetensors"


def build_flux_workflow(
    prompt: str,
    *,
    width: int = 768,
    height: int = 768,
    steps: int = 4,
    seed: int = 0,
) -> dict:
    return {
        "ckpt": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": CKPT_NAME},
        },
        "pos": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["ckpt", 1]},
        },
        "neg": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": ["ckpt", 1]},
        },
        "latent": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["ckpt", 0],
                "positive": ["pos", 0],
                "negative": ["neg", 0],
                "latent_image": ["latent", 0],
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["ckpt", 2]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "lineart", "images": ["decode", 0]},
        },
    }
