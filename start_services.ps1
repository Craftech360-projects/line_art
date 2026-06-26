<#
  start_services.ps1 - bring up the two Docker services the line_art server needs.

  Lightweight "start if not already running" launcher (NOT full setup - use
  setup_comfyui.ps1 / setup_speaches.ps1 once for the A-to-Z install + model
  download). This script:

    1. Checks Docker Desktop is running; if not, tells you to start it and stops.
    2. Ensures the ComfyUI container (with flux1-schnell-fp8.safetensors) is up.
    3. Ensures the Speaches container is up.

  Idempotent: if a container is already healthy, it is left alone.

  Usage:  powershell -ExecutionPolicy Bypass -File .\start_services.ps1
#>

# Continue (not Stop): docker prints benign warnings to stderr that would abort
# the script under -Stop. We check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"

$ComfyPort   = 8188
$SpeachesPort = 8001
$Ckpt     = "flux1-schnell-fp8.safetensors"
$CkptPath = Join-Path $PSScriptRoot "comfyui-data\models\checkpoints\$Ckpt"

function Info($m) { Write-Host "[start] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[start] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[start] $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "[start] $m" -ForegroundColor Red }

# Is a container with this name currently running?
function Container-Running($name) {
    $out = & cmd /c "docker ps --filter name=^/$name`$ --filter status=running --format {{.Names}} 2>nul"
    return ($out -split "`n" | ForEach-Object { $_.Trim() }) -contains $name
}

# Does an HTTP endpoint answer within a few retries? Any HTTP response (incl. a
# 404) means the server is bound and listening, so that counts as "up".
function Wait-Http($url, $label, $retries = 30) {
    foreach ($i in 1..$retries) {
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 *> $null
            return $true
        } catch {
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                return $true   # got an HTTP status (e.g. 404) => server is listening
            }
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

# ---------------------------------------------------------------------------
# 1. Docker Desktop running?  (pause + re-check loop instead of exiting)
# ---------------------------------------------------------------------------
Info "Checking Docker Desktop..."
& cmd /c "docker info >nul 2>&1"
while ($LASTEXITCODE -ne 0) {
    Err "Docker Desktop is NOT running."
    Warn "Start Docker Desktop, wait until it says 'Engine running', then press Enter to re-check."
    [void](Read-Host "Press Enter once Docker is running (or Ctrl+C to abort)")
    Info "Re-checking Docker..."
    & cmd /c "docker info >nul 2>&1"
}
Ok "Docker is running."

# ---------------------------------------------------------------------------
# 2. ComfyUI (with the FLUX checkpoint)
# ---------------------------------------------------------------------------
if (Container-Running "comfyui") {
    Ok "ComfyUI container already running."
} else {
    Info "ComfyUI container is not running - starting it..."
    if (-not (Test-Path $CkptPath)) {
        Err "Checkpoint missing: $CkptPath"
        Err "Run .\setup_comfyui.ps1 first to download $Ckpt (~17 GB)."
        exit 1
    }
    Ok "Found checkpoint $Ckpt."
    & cmd /c "docker compose up -d comfyui 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Err "docker compose up comfyui failed. If the image isn't built yet, run .\setup_comfyui.ps1."
        exit 1
    }
}

Info "Waiting for ComfyUI API on :$ComfyPort ..."
if (Wait-Http "http://localhost:$ComfyPort/" "comfyui") {
    Ok "ComfyUI API is up."
    # Confirm the checkpoint is actually visible to ComfyUI (so generation works).
    try {
        $info = (Invoke-WebRequest -Uri "http://localhost:$ComfyPort/object_info/CheckpointLoaderSimple" -UseBasicParsing -TimeoutSec 5).Content
        if ($info -like "*$Ckpt*") { Ok "Checkpoint '$Ckpt' is loaded and selectable." }
        else { Warn "Checkpoint not listed yet. Try: docker compose restart comfyui" }
    } catch { Warn "Could not query object_info (API is up; non-fatal)." }
} else {
    Warn "ComfyUI did not answer on :$ComfyPort yet. Check 'docker logs comfyui'."
}

# ---------------------------------------------------------------------------
# 3. Speaches (STT)
# ---------------------------------------------------------------------------
if (Container-Running "speaches") {
    Ok "Speaches container already running."
} else {
    Info "Speaches container is not running - starting it..."
    & cmd /c "docker compose up -d speaches 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Err "docker compose up speaches failed. Run .\setup_speaches.ps1 first if this is a fresh machine."
        exit 1
    }
}

Info "Waiting for Speaches API on :$SpeachesPort ..."
if (Wait-Http "http://localhost:$SpeachesPort/v1/models" "speaches") {
    Ok "Speaches API is up."
} else {
    Warn "Speaches did not answer on :$SpeachesPort yet. Check 'docker logs speaches'."
}

# ---------------------------------------------------------------------------
Ok "DONE - services checked."
Info "ComfyUI : http://localhost:$ComfyPort   (model: $Ckpt)"
Info "Speaches: http://localhost:$SpeachesPort/v1/models"
Warn "First image gen loads the 17 GB FLUX model into VRAM (a few minutes); later ones are ~5 s."
# ---------------------------------------------------------------------------
# 4. Start the app server on :8090
# ---------------------------------------------------------------------------
# Run WITHOUT --reload on purpose: the file watcher reloads on every write
# (including generated_images/) and drops live device WebSocket sessions
# mid-flow. Set SAVE_INPUT_AUDIO=1 here if you want incoming WAVs saved.
$AppPort = 8090

# If something is already serving on :8090, don't launch a second instance.
$already = $false
try {
    Invoke-WebRequest -Uri "http://localhost:$AppPort/static/" -UseBasicParsing -TimeoutSec 3 *> $null
    $already = $true
} catch {
    # A 404 still means something is listening (server up, path just missing).
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) { $already = $true }
}

if ($already) {
    Warn "Something is already listening on :$AppPort - not starting a second app instance."
    Info "If that's a stale/old instance, stop it first, then re-run."
} else {
    Info "Starting the app server on :$AppPort (new window; close it to stop)..."
    # Launch uvicorn in a separate PowerShell window so its logs are visible and
    # this script can finish. Working dir = the project root.
    $cmd = "Set-Location '$PSScriptRoot'; python -m uvicorn app.main:app --host 0.0.0.0 --port $AppPort"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    if (Wait-Http "http://localhost:$AppPort/static/" "app" 30) {
        Ok "App server is up on http://localhost:$AppPort (see the new window for logs)."
    } else {
        # /static/ 404s but the socket is open once uvicorn binds; treat reachable as up.
        Warn "App not confirmed on :$AppPort yet - check the new window's logs."
    }
}

Info "Device/browser WebSocket endpoint: ws://localhost:$AppPort/ws"
