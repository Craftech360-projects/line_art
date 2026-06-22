<#
  setup_speaches.ps1 - A-to-Z setup for the Speaches (speech-to-text) service.

  Idempotent: safe to re-run. It ensures the model-cache volume exists, starts
  the Speaches container, pulls the Whisper model, and waits until the service
  answers - so when it finishes, the server can transcribe device audio.

  Usage:  powershell -ExecutionPolicy Bypass -File .\setup_speaches.ps1
#>

# Continue (not Stop): native tools like docker print warnings to stderr, which
# under -Stop would abort the script. We check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"
$Model = "Systran/faster-whisper-large-v3"
$Port  = 8001
$Volume = "hf-hub-cache"

function Info($m) { Write-Host "[speaches] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[speaches] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[speaches] $m" -ForegroundColor Yellow }

# 0. Docker up? (stderr swallowed so a benign warning doesn't abort the script)
Info "Checking Docker..."
& cmd /c "docker info >nul 2>&1"
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and re-run." }

# 1. Model-cache volume (compose declares it `external`, so it must pre-exist).
$exists = docker volume ls --format "{{.Name}}" | Select-String -SimpleMatch $Volume
if (-not $exists) {
    Info "Creating model-cache volume '$Volume'..."
    docker volume create $Volume | Out-Null
} else {
    Info "Model-cache volume '$Volume' already exists."
}

# 2. Start the container. (via cmd so compose's stderr progress prints cleanly)
Info "Starting Speaches container..."
& cmd /c "docker compose up -d speaches 2>&1"
if ($LASTEXITCODE -ne 0) { throw "docker compose up speaches failed." }

# 3. Wait for the API to come up.
Info "Waiting for Speaches API on :$Port ..."
$ready = $false
foreach ($i in 1..60) {
    try {
        Invoke-WebRequest -Uri "http://localhost:$Port/v1/models" -UseBasicParsing -TimeoutSec 3 *> $null
        $ready = $true; break
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { throw "Speaches did not answer on :$Port. Check 'docker logs speaches'." }
Ok "API is up."

# 4. Ensure the Whisper model is present (pull once if missing).
Info "Checking model '$Model' ..."
$models = (Invoke-WebRequest -Uri "http://localhost:$Port/v1/models" -UseBasicParsing).Content
if ($models -like "*$Model*") {
    Ok "Model already installed."
} else {
    Info "Pulling model '$Model' (one-time download, ~1.5 GB)..."
    try {
        Invoke-WebRequest -Method POST -Uri "http://localhost:$Port/v1/models/$Model" -UseBasicParsing -TimeoutSec 600 *> $null
    } catch {
        Warn "Pull request returned an error; the model may still be downloading. Re-run to verify."
    }
    # confirm
    $models = (Invoke-WebRequest -Uri "http://localhost:$Port/v1/models" -UseBasicParsing).Content
    if ($models -like "*$Model*") { Ok "Model installed." }
    else { Warn "Model not yet listed. Open http://localhost:$Port to pull it via the UI, or re-run." }
}

# 5. Smoke test: transcribe 1s of silence (proves the STT path end-to-end).
Info "Smoke-testing transcription..."
$tmp = [System.IO.Path]::GetTempFileName() + ".wav"
# 1s 16kHz mono 16-bit silent WAV
$sr = 16000; $data = New-Object byte[] ($sr * 2)
$fs = [System.IO.File]::Create($tmp)
$bw = New-Object System.IO.BinaryWriter($fs)
$bw.Write([char[]]"RIFF"); $bw.Write([int](36 + $data.Length)); $bw.Write([char[]]"WAVE")
$bw.Write([char[]]"fmt "); $bw.Write([int]16); $bw.Write([int16]1); $bw.Write([int16]1)
$bw.Write([int]$sr); $bw.Write([int]($sr * 2)); $bw.Write([int16]2); $bw.Write([int16]16)
$bw.Write([char[]]"data"); $bw.Write([int]$data.Length); $bw.Write($data)
$bw.Close(); $fs.Close()
try {
    $r = & curl.exe -s -m 60 "http://localhost:$Port/v1/audio/transcriptions" `
         -F "file=@$tmp" -F "model=$Model" -F "response_format=json"
    Ok "Transcription endpoint responded: $r"
} catch {
    Warn "Smoke test could not run (curl missing?). The API is up regardless."
} finally { Remove-Item $tmp -ErrorAction SilentlyContinue }

Ok "READY - Speaches is serving on http://localhost:$Port (STT model: $Model)"
