#!/usr/bin/env python3
"""Machine-specific paths for Hankifier and Hankiplier, in one place.

Each setting is resolved in this order:
    1. environment variable HANKIFIER_<NAME>   (e.g. HANKIFIER_RVC_DIR)
    2. the user config file                    (~/.config/hankifier/config.json,
                                                or $HANKIFIER_CONFIG)
    3. the default below

Standard library only, so the UVR and RVC venvs (which run the stage scripts)
can import it too. `python3 hankify_config.py show` prints what everything
resolved to; `python3 hankify_config.py get uvr_python` prints one value.
"""
import json
import os
import sys

PIPELINE_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.path.dirname(PIPELINE_DIR)

CONFIG_FILE = os.environ.get("HANKIFIER_CONFIG") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "hankifier", "config.json")


def _load():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_FILE = _load()


def _expand(p):
    return os.path.expanduser(p) if isinstance(p, str) and p else p


def get(name, default=None):
    """Resolve one setting (env > config file > default); '~' is expanded."""
    value = os.environ.get("HANKIFIER_" + name.upper())
    if value is None:
        value = _FILE.get(name)
    if value is None:
        value = default
    return _expand(value)


WINDOWS = os.name == "nt"
# A venv keeps its interpreter in bin/ on Linux and Scripts\ on Windows.
_VENV_PY = os.path.join("Scripts", "python.exe") if WINDOWS else os.path.join("bin", "python")

# RVC-WebUI checkout (with the patch from patches/ applied) and its venv.
RVC_DIR = get("rvc_dir", "~/RVC-WebUI")
RVC_PYTHON = get("rvc_python", os.path.join(RVC_DIR, ".venv", _VENV_PY))
RVC_WEIGHTS_DIR = get("rvc_weights_dir", os.path.join(RVC_DIR, "assets", "weights"))
RVC_INDICES_DIR = get("rvc_indices_dir", os.path.join(RVC_DIR, "assets", "indices"))

# Ultimate Vocal Remover checkout (with the patch from patches/ applied) and its venv.
# The Hankify / Hankiplier GUIs also run in this venv.
UVR_DIR = get("uvr_dir", "~/ultimatevocalremovergui")
UVR_PYTHON = get("uvr_python", os.path.join(UVR_DIR, ".venv", _VENV_PY))

# Stem separation backend: "uvr" drives the patched UVR5 GUI headlessly (the Linux setup);
# "audio-separator" runs the same UVR models through the pip package python-audio-separator
# (no UVR checkout needed; what the Windows installer sets up).
SEPARATOR = get("separator", "uvr")
# Where audio-separator keeps its downloaded models.
SEPARATOR_MODELS_DIR = get("separator_models_dir", "~/.cache/hankifier/separator-models")

# Finished MP3/MP4 files land here.
OUTPUT_DIR = get("output_dir", "~/Music/hankify")
# Per-run scratch folders (stems etc.); deleted after each run unless "keep intermediates".
RUNS_DIR = get("runs_dir", "~/.cache/hankifier/pipeline_runs")
# Kept stems (Hankiplier reads these).
INTERMEDIATES_DIR = get("intermediates_dir", os.path.join(OUTPUT_DIR, "intermediates"))

# Optional still image used for MP4s when none is chosen in the GUI.
FALLBACK_IMAGE = get("fallback_image")

PIPELINE_SCRIPT = os.path.join(PIPELINE_DIR, "pipeline.py")
HANKIFY_APP = os.path.join(REPO_DIR, "hankify", "app.py")

_SHOWN = ["rvc_dir", "rvc_python", "rvc_weights_dir", "rvc_indices_dir", "uvr_dir", "uvr_python",
          "separator", "separator_models_dir", "output_dir", "runs_dir", "intermediates_dir", "fallback_image"]


def resolved():
    g = globals()
    return {k: g[k.upper()] for k in _SHOWN}


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["show"]:
        print("config file:", CONFIG_FILE, "(found)" if _FILE else "(missing or empty)")
        for k, v in resolved().items():
            print(f"{k:18} {v}")
    elif cmd == ["get"] and len(sys.argv) == 3 and sys.argv[2] in _SHOWN:
        print(resolved()[sys.argv[2]] or "")
    else:
        sys.exit("usage: hankify_config.py show | get <" + "|".join(_SHOWN) + ">")
