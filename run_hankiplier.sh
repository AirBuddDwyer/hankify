#!/usr/bin/env bash
# Launch the Hankiplier GUI (multi-voice Hankify) inside the UVR venv, like Hankify. Its engine runs in the RVC venv.
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$(python3 "$HERE/song-pipeline/hankify_config.py" get uvr_python)"
[ -x "$PY" ] || { echo "UVR venv python not found: $PY (see SETUP.md, 'Config')" >&2; exit 1; }
cd "$HERE/hankiplier" && exec "$PY" app.py "$@"
