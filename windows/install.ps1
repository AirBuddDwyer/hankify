# Hankify installer for Windows. Run it through "Install Hankify.bat" (double-click).
#
# Everything lands inside this folder (Python, packages, models, ffmpeg); nothing is installed
# system-wide and nothing needs admin rights. To uninstall, delete the folder.
# What it downloads, all from the projects' official sources:
#   uv (Python/package manager, github.com/astral-sh/uv), Python 3.12 (via uv), PyTorch (pytorch.org),
#   the pinned Python packages (pypi.org), RVC-WebUI at a pinned commit (github.com/RVC-Project),
#   RVC's base models + the two UVR separation models (huggingface.co / github.com), Whisper "small" for
#   Hankiplier (openaipublic.azureedge.net, the URL the openai-whisper package itself uses),
#   ffmpeg (gyan.dev builds, linked from ffmpeg.org) if it isn't already installed.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"      # Invoke-WebRequest is very slow with the progress bar on
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$Tools = Join-Path $Root "tools"
$RvcDir = Join-Path $Root "RVC"
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

$UvVersion = "0.13.5"
$RvcCommit = "81eed5e8f68b6bed1789f682fe78cdd324495afc"
$TorchVersion = "2.7.1"
$TorchvisionVersion = "0.22.1"

# uv keeps its Python builds and download cache inside this folder too.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR = Join-Path $Tools "uv-cache"
$env:UV_LINK_MODE = "copy"
$env:PYTHONUTF8 = "1"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

# Python packages have deep folder trees; Windows' 260-character path limit breaks installs in deep folders,
# and OneDrive tries to sync the several GB this downloads.
if ($Root.Length -gt 60 -or $Root -match "OneDrive") {
    Write-Host "Hankify is in: $Root" -ForegroundColor Yellow
    Write-Host "That folder is too deep or inside OneDrive, which can break the install." -ForegroundColor Yellow
    Write-Host "Move the whole Hankify folder somewhere short like C:\Hankify and run the installer from there." -ForegroundColor Yellow
    $answer = Read-Host "Type Y to try anyway, or just press Enter to stop"
    if ($answer -ne "Y" -and $answer -ne "y") { exit 1 }
}

function Download($url, $dest) {
    Write-Host "    downloading $url"
    for ($try = 1; $try -le 3; $try++) {
        try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing; return }
        catch { if ($try -eq 3) { throw } ; Write-Host "    retrying ($try)..."; Start-Sleep 5 }
    }
}

New-Item -ItemType Directory -Force -Path $Tools | Out-Null

# --- 1. uv ------------------------------------------------------------------------------------------
$Uv = Join-Path $Tools "uv\uv.exe"
if (-not (Test-Path $Uv)) {
    Step "Getting uv $UvVersion"
    $zip = Join-Path $Tools "uv.zip"
    Download "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $Tools "uv") -Force
    Remove-Item $zip
}

# --- 2. ffmpeg ---------------------------------------------------------------------------------------
$FfmpegBin = Join-Path $Tools "ffmpeg\bin"
if (-not (Test-Path (Join-Path $FfmpegBin "ffmpeg.exe"))) {
    if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        Step "Using the ffmpeg already installed on this PC"
    } else {
        Step "Getting ffmpeg"
        $zip = Join-Path $Tools "ffmpeg.zip"
        Download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
        $tmp = Join-Path $Tools "ffmpeg-unpack"
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $bin = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $FfmpegBin | Out-Null
        Copy-Item (Join-Path $bin.DirectoryName "*.exe") $FfmpegBin
        Remove-Item $zip; Remove-Item $tmp -Recurse -Force
    }
}

$env:PATH = "$FfmpegBin;$env:PATH"      # the model download and self-test below run ffmpeg too

# --- 3. RVC-WebUI (pinned commit + Hankify's patch, shipped as patched files) -----------------------
if (-not (Test-Path (Join-Path $RvcDir "infer\vc\modules.py"))) {
    Step "Getting RVC-WebUI"
    $zip = Join-Path $Tools "rvc.zip"
    Download "https://codeload.github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/zip/$RvcCommit" $zip
    $tmp = Join-Path $Tools "rvc-unpack"
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    Move-Item $src.FullName $RvcDir
    Remove-Item $zip; Remove-Item $tmp -Recurse -Force
}
Copy-Item -Path (Join-Path $Root "windows\rvc-overlay\*") -Destination $RvcDir -Recurse -Force
foreach ($d in "assets\weights", "assets\indices", "assets\hubert_base", "assets\rmvpe") {
    New-Item -ItemType Directory -Force -Path (Join-Path $RvcDir $d) | Out-Null
}

# --- 4. Python + packages ----------------------------------------------------------------------------
if (-not (Test-Path $Py)) {
    Step "Setting up Python 3.12"
    # uv's own Python build (always has tkinter), not whatever Python this PC may already have
    & $Uv venv --python 3.12 --managed-python $Venv
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}

# Pick the PyTorch build for this PC's graphics card.
$TorchIndex = "https://download.pytorch.org/whl/cpu"
$Gpu = "none (CPU only - this will be slow, several minutes per song)"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $name = $null
    try { $name = (& nvidia-smi --query-gpu=name --format=csv,noheader | Select-Object -First 1) } catch { }
    if ($name) {
        $Gpu = "$name (NVIDIA)"
        if ($name -match "RTX 50\d\d") { $TorchIndex = "https://download.pytorch.org/whl/cu128" }
        else { $TorchIndex = "https://download.pytorch.org/whl/cu118" }
    }
}
Step "Installing PyTorch for: $Gpu  (large download, be patient)"
& $Uv pip install --python $Py "torch==$TorchVersion" "torchaudio==$TorchVersion" "torchvision==$TorchvisionVersion" --index-url $TorchIndex
if ($LASTEXITCODE -ne 0) { throw "PyTorch install failed" }

Step "Installing the other Python packages"
& $Uv pip install --python $Py -r (Join-Path $Root "windows\requirements-windows.txt") --index-url https://pypi.org/simple
if ($LASTEXITCODE -ne 0) { throw "package install failed" }

# --- 5. Settings ---------------------------------------------------------------------------------------
$OutDir = Join-Path ([Environment]::GetFolderPath("MyMusic")) "Hankify"
$Config = [ordered]@{
    rvc_dir              = $RvcDir
    rvc_python           = $Py
    uvr_dir              = $Root
    uvr_python           = $Py
    separator            = "audio-separator"
    separator_models_dir = (Join-Path $Root "models\separator")
    output_dir           = $OutDir
    runs_dir             = (Join-Path $Root "work\runs")
}
$ConfigPath = Join-Path $Root "config.json"
if (-not (Test-Path $ConfigPath)) {
    # UTF-8 without a BOM (PowerShell 5's -Encoding UTF8 adds one, which Python's json module rejects)
    [IO.File]::WriteAllText($ConfigPath, ($Config | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
$env:HANKIFIER_CONFIG = $ConfigPath
# Hankiplier's Whisper model stays inside this folder too (Hankiplier.bat sets the same variable).
$env:HANKIPLIER_WHISPER_DIR = Join-Path $Root "models\whisper"

# --- 6. Models + voices ----------------------------------------------------------------------------------
Step "Downloading the RVC base models, the vocal separation models and Whisper (for Hankiplier)"
& $Py -I (Join-Path $Root "windows\prefetch_models.py")
if ($LASTEXITCODE -ne 0) { throw "model download failed" }

# Voices dropped in the voices folder (downloaded .zip files, or .pth + .index): same importer as the
# "Import Voice..." button in Hankify. Already-installed ones are skipped.
$Voices = Join-Path $Root "voices"
& $Py -I (Join-Path $Root "song-pipeline\voice_import.py") $Voices

Step "Checking the install"
& $Py -I (Join-Path $Root "windows\selftest.py")
if ($LASTEXITCODE -ne 0) { throw "self-test failed (see above)" }

Write-Host ""
Write-Host "All done. Start it with Hankify.bat (add voices there with Import Voice...)." -ForegroundColor Green
Write-Host "Multi-voice songs (one song, several singers): Hankiplier.bat."
Write-Host "Finished songs go to: $OutDir"
