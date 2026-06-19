# Minimal ComfyUI image: official ComfyUI on a CUDA PyTorch base, run as root
# with a plain host bind mount for /ComfyUI/models and /ComfyUI/output. This
# avoids the UID-ownership enforcement of wrapper images that breaks on Windows
# bind mounts.
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /ComfyUI
WORKDIR /ComfyUI
RUN pip install -r requirements.txt

EXPOSE 8188
# --listen binds all interfaces so the host can reach the API on 8188.
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
