import os

# --- Provider selection (per-service local<->cloud switch) ---
# STT_BACKEND:   "groq"  (cloud Whisper)      | "local" (Speaches server)
# IMAGE_BACKEND: "hf"    (cloud FLUX via HF)  | "comfyui" (local ComfyUI server)
# Defaults are CLOUD so nothing changes unless you opt in.
STT_BACKEND = os.environ.get("STT_BACKEND", "groq").lower()
IMAGE_BACKEND = os.environ.get("IMAGE_BACKEND", "hf").lower()
# MODERATION_BACKEND: "groq" (LLM child-safety check) | "off" (keyword filter only).
# Separate from GROQ_API_KEY so you can disable moderation WITHOUT breaking Groq STT.
MODERATION_BACKEND = os.environ.get("MODERATION_BACKEND", "groq").lower()
# Fallback image served when image GENERATION fails (e.g. ComfyUI/HF down), so the device
# still gets a picture. Path is relative to the server's working dir. Empty = no fallback
# (the error propagates). Safety blocks are NEVER replaced by the fallback.
IMAGINE_FALLBACK_IMAGE = os.environ.get("IMAGINE_FALLBACK_IMAGE", "fallback.jpg")

# Local providers (used when the backends above are set to local/comfyui).
SPEACHES_BASE_URL = os.environ.get("SPEACHES_BASE_URL", "http://localhost:8001")
SPEACHES_MODEL = os.environ.get("SPEACHES_MODEL", "Systran/faster-whisper-large-v3")
COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://localhost:8188")
# Max seconds line_art waits for ComfyUI before giving up (and using the fallback image).
# Keep this BELOW the gateway's IMAGINE_TIMEOUT_MS (default 90 s) so line_art always
# resolves (image or fallback) before the gateway times out the request.
COMFYUI_TIMEOUT_S = float(os.environ.get("COMFYUI_TIMEOUT_S", "60"))

# Cloud backends: Groq Whisper for STT, HuggingFace FLUX for image generation.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3")
# Fast LLM used for AI Imagine child-safety moderation (see app/moderation.py).
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
HF_API_TOKEN = os.environ.get("HF_API_TOKEN") or os.environ.get("HUGGINGFACE_API_TOKEN")
HF_MODEL_URL = os.environ.get(
    "HF_MODEL_URL",
    "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
)
