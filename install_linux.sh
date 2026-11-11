#!/usr/bin/env bash
# Hankify installer for Linux: the counterpart of "Install Hankify.bat" on Windows.
#
#   ./install_linux.sh              install (or repair/update) everything into this folder
#   ./install_linux.sh --cpu        force the CPU build of PyTorch (no NVIDIA card, or to save disk)
#   ./install_linux.sh --cuda 118   force a CUDA build (118 = most NVIDIA cards, 128 = RTX 50-series)
#   ./install_linux.sh --desktop    also add Hankify + Hankiplier to your app launcher
#
# Everything lands inside this folder (uv, Python 3.12, packages, RVC, models), the same layout the Windows
# installer uses; nothing is installed system-wide and it never runs sudo. If a system package is missing
# (ffmpeg, curl, tar) it prints the exact command for your distro and stops. To uninstall, delete the folder
# (and the two .desktop files if you used --desktop).
# What it downloads, all from the projects' official sources:
#   uv (github.com/astral-sh/uv, checksum-verified), Python 3.12 (via uv, includes tkinter), PyTorch
#   (download.pytorch.org), the pinned Python packages (pypi.org), RVC-WebUI at a pinned commit
#   (github.com/RVC-Project) plus Hankify's patched files, RVC's base models and the two UVR separation
#   models (huggingface.co / github.com). It never downloads or bundles any voice model.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TOOLS="$ROOT/tools"
RVC_DIR="$ROOT/RVC"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
CONFIG="$ROOT/config.json"

UV_VERSION="0.13.5"
UV_SHA256_X86_64="f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f"
RVC_COMMIT="81eed5e8f68b6bed1789f682fe78cdd324495afc"
TORCH_VERSION="2.7.1"
TORCHVISION_VERSION="0.22.1"
AUDIO_SEPARATOR_VERSION="0.30.2"

TORCH_MODE="auto"
DESKTOP=0

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --cpu) TORCH_MODE="cpu" ;;
        --cuda)
            shift
            case "${1:-}" in 118|128) TORCH_MODE="cu$1" ;; *) echo "--cuda takes 118 or 128" >&2; exit 2 ;; esac ;;
        --desktop) DESKTOP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# uv keeps its Python builds and download cache inside this folder too.
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_CACHE_DIR="$TOOLS/uv-cache"
export UV_LINK_MODE=copy
export HANKIFIER_CONFIG="$CONFIG"

download() {   # download URL DEST, with retries
    local url="$1" dest="$2" try
    note "downloading $url"
    for try in 1 2 3; do
        if curl -fL --retry 2 -o "$dest.part" "$url"; then mv "$dest.part" "$dest"; return 0; fi
        note "retrying ($try)..."; sleep 5
    done
    rm -f "$dest.part"; die "download failed: $url"
}

# --- 0. System check ---------------------------------------------------------------------------------
step "Checking this machine"
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux (Windows: use \"Install Hankify.bat\")"
[ "$(uname -m)" = "x86_64" ] || die "only x86_64 (64-bit Intel/AMD) Linux is supported, this is $(uname -m)"

missing=()
for c in curl tar sha256sum ffmpeg ffprobe; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
    pkgs="ffmpeg curl tar coreutils"
    if command -v apt-get >/dev/null; then cmd="sudo apt-get install -y $pkgs"
    elif command -v pacman >/dev/null; then cmd="sudo pacman -S --needed $pkgs"
    elif command -v dnf >/dev/null; then cmd="sudo dnf install -y ffmpeg-free curl tar coreutils   # or ffmpeg from RPM Fusion"
    elif command -v zypper >/dev/null; then cmd="sudo zypper install -y $pkgs"
    else cmd="(install these with your distro's package manager: $pkgs)"; fi
    die "missing: ${missing[*]}
    Install them, then run this script again:
        $cmd"
fi
encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"    # not piped into grep -q: pipefail + SIGPIPE
if [[ "$encoders" != *libmp3lame* ]]; then
    die "your ffmpeg has no MP3 encoder (libmp3lame). Install your distro's full ffmpeg package and run this again."
fi
note "ok: $(ffmpeg -version | head -1 | cut -d' ' -f1-3)"
mkdir -p "$TOOLS"

# --- 1. uv -------------------------------------------------------------------------------------------
UV="$TOOLS/uv/uv"
if [ ! -x "$UV" ] || [ "$("$UV" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
    step "Getting uv $UV_VERSION"
    tgz="$TOOLS/uv.tar.gz"
    download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" "$tgz"
    echo "$UV_SHA256_X86_64  $tgz" | sha256sum -c --quiet - || { rm -f "$tgz"; die "uv download failed its checksum"; }
    rm -rf "$TOOLS/uv"; mkdir -p "$TOOLS/uv"
    tar -xzf "$tgz" -C "$TOOLS/uv" --strip-components=1
    rm -f "$tgz"
fi

# --- 2. RVC-WebUI (pinned commit + Hankify's patch, shipped as patched files) ------------------------
if [ ! -f "$RVC_DIR/infer/vc/modules.py" ]; then
    step "Getting RVC-WebUI"
    tgz="$TOOLS/rvc.tar.gz"
    download "https://codeload.github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/tar.gz/$RVC_COMMIT" "$tgz"
    rm -rf "$RVC_DIR.part"; mkdir -p "$RVC_DIR.part"
    tar -xzf "$tgz" -C "$RVC_DIR.part" --strip-components=1
    rm -rf "$RVC_DIR"; mv "$RVC_DIR.part" "$RVC_DIR"; rm -f "$tgz"
fi
cp -r "$ROOT/windows/rvc-overlay/." "$RVC_DIR/"          # same patched files the Windows install uses
mkdir -p "$RVC_DIR/assets/weights" "$RVC_DIR/assets/indices" "$RVC_DIR/assets/hubert_base" "$RVC_DIR/assets/rmvpe"

# --- 3. Python + packages ----------------------------------------------------------------------------
if [ ! -x "$PY" ]; then
    step "Setting up Python 3.12"
    # uv's own Python build (it includes tkinter), not whatever Python this machine may already have
    "$UV" venv --python 3.12 --managed-python "$VENV"
fi

gpu_name=""
if command -v nvidia-smi >/dev/null; then
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)"
fi
case "$TORCH_MODE" in
    cpu) build="cpu" ;;
    cu118|cu128) build="$TORCH_MODE" ;;
    auto)
        if [ -z "$gpu_name" ]; then build="cpu"
        elif echo "$gpu_name" | grep -Eq "RTX 50[0-9]{2}"; then build="cu128"
        else build="cu118"; fi ;;
esac
if [ "$build" = "cpu" ]; then
    gpu_desc="none (CPU only: this works, but expect several minutes per song)"
    [ -n "$gpu_name" ] && gpu_desc="CPU build chosen (card: $gpu_name)"
else
    gpu_desc="${gpu_name:-NVIDIA (forced)} -> CUDA ${build#cu}"
fi
step "Installing PyTorch for: $gpu_desc  (large download, be patient)"
"$UV" pip install --python "$PY" "torch==$TORCH_VERSION" "torchaudio==$TORCH_VERSION" \
    "torchvision==$TORCHVISION_VERSION" --index-url "https://download.pytorch.org/whl/$build"

step "Installing the other Python packages"
"$UV" pip install --python "$PY" -r "$ROOT/linux/requirements-linux.txt" --index-url https://pypi.org/simple
# audio-separator without its declared deps: on Linux it asks for "diffq", which has no Python 3.12 wheel; its real
# dependencies (with diffq-fixed, the same module) are already in requirements-linux.txt.
"$UV" pip install --python "$PY" --no-deps "audio-separator==$AUDIO_SEPARATOR_VERSION" --index-url https://pypi.org/simple

# --- 4. Settings (config.json in this folder; an existing one is kept, missing keys are added) -------
step "Writing settings"
music_dir="$(xdg-user-dir MUSIC 2>/dev/null || true)"
[ -n "$music_dir" ] && [ "$music_dir" != "$HOME" ] || music_dir="$HOME/Music"
"$PY" -I - "$CONFIG" "$ROOT" "$music_dir/Hankify" <<'EOF'
import json, os, shutil, sys, time
path, root, out = sys.argv[1:4]
want = {
    "rvc_dir": os.path.join(root, "RVC"),
    "rvc_python": os.path.join(root, ".venv", "bin", "python"),
    "uvr_dir": root,
    "uvr_python": os.path.join(root, ".venv", "bin", "python"),
    "separator": "audio-separator",
    "separator_models_dir": os.path.join(root, "models", "separator"),
    "output_dir": out,
    "runs_dir": os.path.join(root, "work", "runs"),
}
have = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            have = json.load(f)
    except ValueError:
        backup = f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"    existing config.json was not valid JSON; kept a copy at {backup}")
added = [k for k in want if k not in have]
if added or not os.path.exists(path):
    merged = {**want, **have}            # the user's own values win
    with open(path + ".tmp", "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(path + ".tmp", path)
    print(f"    {path}: " + ("written" if not have else "added " + ", ".join(added)))
else:
    print(f"    {path}: kept as is")
for k in want:
    if k in have and have[k] != want[k]:
        print(f"    note: {k} = {have[k]} (your value; the default for this install is {want[k]})")
EOF

# --- 5. Models + voices ------------------------------------------------------------------------------
step "Downloading the RVC base models and the vocal separation models"
"$PY" -I "$ROOT/windows/prefetch_models.py"

# Voices dropped in the voices folder (downloaded .zip files, or .pth + .index): same importer as the
# "Import Voice..." button in Hankify. Already-installed ones are skipped.
mkdir -p "$ROOT/voices"
"$PY" -I "$ROOT/song-pipeline/voice_import.py" "$ROOT/voices" || note "(voice import reported a problem, see above)"

# --- 6. Launcher entries (optional) ------------------------------------------------------------------
if [ "$DESKTOP" = 1 ]; then
    step "Adding Hankify and Hankiplier to the app launcher"
    apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$apps"
    for t in "$ROOT"/desktop/*.desktop.template; do
        name="$(basename "$t" .template)"
        sed -e "s|^Exec=@REPO@/\(.*\)$|Exec=\"$ROOT/\1\"|" -e "s|@REPO@|$ROOT|g" "$t" > "$apps/$name"
        note "$apps/$name"
    done
fi

# --- 7. Self-test ------------------------------------------------------------------------------------
step "Checking the install"
"$PY" -I "$ROOT/windows/selftest.py" || die "self-test failed (see above)"

printf '\n\033[1;32mAll done.\033[0m Start it with ./run_hankify.sh (multi-voice: ./run_hankiplier.sh).\n'
echo "Add voices in Hankify with Import Voice..., or drop them in voices/ and run this script again."
echo "Finished songs go to: $music_dir/Hankify"
