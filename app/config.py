import os

SPEACHES_BASE_URL = os.environ.get("SPEACHES_BASE_URL", "http://localhost:8001")
SPEACHES_MODEL = os.environ.get("SPEACHES_MODEL", "Systran/faster-whisper-large-v3")
COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://localhost:8188")
