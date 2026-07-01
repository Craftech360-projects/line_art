import os

# Cloud backends (same as the main branch): Groq Whisper for STT, HuggingFace
# FLUX for image generation.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3")
# Fast LLM used for AI Imagine child-safety moderation (see app/moderation.py).
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
HF_API_TOKEN = os.environ.get("HF_API_TOKEN") or os.environ.get("HUGGINGFACE_API_TOKEN")
HF_MODEL_URL = os.environ.get(
    "HF_MODEL_URL",
    "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
)
