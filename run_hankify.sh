#!/usr/bin/env bash
# Launch the Hankify GUI inside the UVR venv (that venv has tkinterdnd2 + Pillow).
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PY="$(python3 "$HERE/song-pipeline/hankify_config.py" get uvr_python)"
[ -x "$PY" ] || { echo "UVR venv python not found: $PY (see SETUP.md, 'Config')" >&2; exit 1; }
cd "$HERE/hankify" && exec "$PY" app.py "$@"
