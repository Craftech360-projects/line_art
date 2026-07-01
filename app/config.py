import os

# --- Provider selection (per-service local<->cloud switch) ---
# STT_BACKEND:   "groq"  (cloud Whisper)      | "local" (Speaches server)
# IMAGE_BACKEND: "hf"    (cloud FLUX via HF)  | "comfyui" (local ComfyUI server)
# Defaults are CLOUD so nothing changes unless you opt in.
STT_BACKEND = os.environ.get("STT_BACKEND", "groq").lower()
IMAGE_BACKEND = os.environ.get("IMAGE_BACKEND", "hf").lower()

# Local providers (used when the backends above are set to local/comfyui).
SPEACHES_BASE_URL = os.environ.get("SPEACHES_BASE_URL", "http://localhost:8001")
SPEACHES_MODEL = os.environ.get("SPEACHES_MODEL", "Systran/faster-whisper-large-v3")
COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://localhost:8188")

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
