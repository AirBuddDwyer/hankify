"""Locate the Hankifier checkout and load its shared path config.

Hankiplier lives in the hankiplier/ folder of the Hankify repo and builds on its Hankify GUI module and
song-pipeline/. The Hankify checkout is found via, in order:
    1. env var HANKIFIER_DIR
    2. "hankifier_dir" in ~/.config/hankifier/config.json (or $HANKIFIER_CONFIG)
    3. the repo root (the parent of this folder)
    4. a sibling folder named "hankifier" (older two-repo layout)
Everything else (RVC/UVR/output paths) comes from Hankifier's hankify_config.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))


def _config_value(key):
    path = os.environ.get("HANKIFIER_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "hankifier", "config.json")
    try:
        with open(path) as f:
            return json.load(f).get(key)
    except (OSError, ValueError):
        return None


def find_hankifier():
    candidates = [os.environ.get("HANKIFIER_DIR"), _config_value("hankifier_dir"),
                  os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "hankifier")]
    for c in candidates:
        c = os.path.expanduser(c) if c else c
        if c and os.path.isfile(os.path.join(c, "song-pipeline", "hankify_config.py")):
            return os.path.realpath(c)
    sys.exit("Hankiplier needs the rest of the Hankify repo (https://github.com/AirBuddDwyer/hankify): run it from\n"
             "the hankiplier/ folder of a full checkout, or set HANKIFIER_DIR / \"hankifier_dir\" in ~/.config/hankifier/config.json.")


HANKIFIER_DIR = find_hankifier()
PIPELINE_DIR = os.path.join(HANKIFIER_DIR, "song-pipeline")
sys.path.insert(0, PIPELINE_DIR)
import hankify_config as cfg  # noqa: E402,F401
