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
COMFYUI_TIMEOUT_S = float(os.environ.get("COMFYUI_TIMEOUT_S", "20"))

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

# --- Manager-api STT provider selection (ADR-0002) ---
# Base URL of cheeko-backend manager-api. Empty => skip manager fetch, use last-resort only.
MANAGER_API_BASE_URL = os.environ.get("MANAGER_API_BASE_URL", "").rstrip("/")
# Service key for backend-to-backend auth (X-Service-Key -> requireAdmin god-mode).
SERVICE_SECRET_KEY = os.environ.get("SERVICE_SECRET_KEY", "")
# How long line_art caches the active provider before refetching (seconds).
STT_PROVIDER_TTL_S = float(os.environ.get("STT_PROVIDER_TTL_S", "300"))
# Fixed env last-resort provider used when the active provider can't serve.
STT_LAST_RESORT_PROVIDER = os.environ.get("STT_LAST_RESORT_PROVIDER", "groq").lower()
# Extra keys so deepgram/sarvam can be the last-resort or used in dev.
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# Save every generated image to generated_images/ (children's data — default OFF in prod).
SAVE_GENERATED_IMAGES = os.environ.get("SAVE_GENERATED_IMAGES", "").lower() in ("1", "true", "yes")
