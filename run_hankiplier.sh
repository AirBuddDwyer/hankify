#!/usr/bin/env bash
# Launch the Hankiplier GUI (multi-voice Hankify) inside the UVR venv, like Hankify. Its engine runs in the RVC venv.
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# install_linux.sh keeps its settings in config.json next to this script; the manual setup uses ~/.config/hankifier/.
if [ -z "${HANKIFIER_CONFIG:-}" ] && [ -f "$HERE/config.json" ]; then export HANKIFIER_CONFIG="$HERE/config.json"; fi
CFGPY=python3; [ -x "$HERE/.venv/bin/python" ] && CFGPY="$HERE/.venv/bin/python"
PY="$("$CFGPY" "$HERE/song-pipeline/hankify_config.py" get uvr_python)"
[ -x "$PY" ] || { echo "Python for the GUI not found: $PY (run ./install_linux.sh, or see SETUP.md, 'Config')" >&2; exit 1; }
cd "$HERE/hankiplier" && exec "$PY" app.py "$@"
