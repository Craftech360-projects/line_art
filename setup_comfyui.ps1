<#
  setup_comfyui.ps1 - A-to-Z setup for the ComfyUI (image generation) service.

  Idempotent: safe to re-run. Creates the model dirs, builds the local ComfyUI
  image, downloads the FLUX.1-schnell fp8 checkpoint (~17 GB, resumable), starts
  the container, and waits until ComfyUI sees the checkpoint - so when it
  finishes, the server can generate line art.

  Usage:  powershell -ExecutionPolicy Bypass -File .\setup_comfyui.ps1
#>

# Continue (not Stop): native tools like docker print warnings to stderr, which
# under -Stop would abort the script. We check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
$Port = 8188
$Ckpt = "flux1-schnell-fp8.safetensors"
$CkptUrl = "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors"
$ExpectedBytes = 17236328572   # ~17.2 GB
$DataDir = Join-Path $PSScriptRoot "comfyui-data"
$CkptDir = Join-Path $DataDir "models\checkpoints"
$CkptPath = Join-Path $CkptDir $Ckpt

function Info($m) { Write-Host "[comfyui] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[comfyui] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[comfyui] $m" -ForegroundColor Yellow }

# 0. Docker up? (run with stderr swallowed so a benign warning doesn't trip -Stop)
Info "Checking Docker..."
& cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and re-run." }

# 1. Data dirs (match the compose bind mounts: ./comfyui-data/models + /output).
Info "Ensuring model directories under $DataDir ..."
New-Item -ItemType Directory -Force -Path $CkptDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "models\vae") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "output") | Out-Null

# 2. Download the FLUX checkpoint (resumable; skip if already complete).
$haveValid = (Test-Path $CkptPath) -and ((Get-Item $CkptPath).Length -eq $ExpectedBytes)
if ($haveValid) {
    Ok "Checkpoint already present and complete ($([math]::Round($ExpectedBytes/1GB,1)) GB)."
} else {
    if (Test-Path $CkptPath) {
        $have = (Get-Item $CkptPath).Length
        Info "Resuming checkpoint download ($([math]::Round($have/1GB,1)) / $([math]::Round($ExpectedBytes/1GB,1)) GB)..."
    } else {
        Info "Downloading FLUX checkpoint (~17 GB; this takes a while)..."
    }
    # curl with resume (-C -) and retries.
    & curl.exe -L -C - --retry 5 --retry-delay 5 -o "$CkptPath" "$CkptUrl"
    $now = (Get-Item $CkptPath).Length
    if ($now -ne $ExpectedBytes) {
        Warn "Downloaded size $now != expected $ExpectedBytes. Re-run to resume."
        throw "Checkpoint download incomplete."
    }
    Ok "Checkpoint downloaded and size-verified."
}

# 3. Build the local ComfyUI image (idempotent; uses build cache).
#    (via cmd so compose's stderr progress prints cleanly under PowerShell.)
Info "Building ComfyUI image (first run installs ComfyUI; later runs are cached)..."
& cmd /c "docker compose build comfyui 2>&1"
if ($LASTEXITCODE -ne 0) { throw "docker compose build comfyui failed." }

# 4. Start the container.
Info "Starting ComfyUI container..."
& cmd /c "docker compose up -d comfyui 2>&1"
if ($LASTEXITCODE -ne 0) { throw "docker compose up comfyui failed." }

# 5. Wait for the HTTP API.
Info "Waiting for ComfyUI API on :$Port ..."
$ready = $false
foreach ($i in 1..60) {
    try {
        Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 3 *> $null
        $ready = $true; break
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { throw "ComfyUI did not answer on :$Port. Check 'docker logs comfyui'." }
Ok "API is up."

# 6. Confirm ComfyUI sees the checkpoint (so generation will work).
Info "Verifying the checkpoint is visible to ComfyUI..."
try {
    $info = (Invoke-WebRequest -Uri "http://localhost:$Port/object_info/CheckpointLoaderSimple" -UseBasicParsing).Content
    if ($info -like "*$Ckpt*") { Ok "Checkpoint '$Ckpt' is loaded and selectable." }
    else { Warn "Checkpoint not listed yet. If you just added it, restart: docker compose restart comfyui" }
} catch {
    Warn "Could not query object_info; the API is up. Check the model folder and restart if needed."
}

# 7. GPU check inside the container.
Info "Checking GPU access in the container..."
try {
    $gpu = docker exec comfyui python -c "import torch; print('cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')" 2>$null
    if ($gpu -like "*cuda True*") { Ok "GPU OK: $gpu" } else { Warn "GPU not detected inside container: $gpu" }
} catch { Warn "Could not run GPU check (non-fatal)." }

Ok "READY - ComfyUI is serving on http://localhost:$Port (model: $Ckpt)"
Warn "Note: the FIRST image generation loads the 17 GB model into VRAM and can take a few minutes; later ones are ~5 s."
