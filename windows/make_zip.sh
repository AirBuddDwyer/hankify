#!/bin/bash
# Build the Windows package: the committed repo (this branch's HEAD) in a top-level "Hankify" folder.
# NO voice models ship in it, ever: users get their own (see "README - START HERE.txt", ADDING VOICES).
#   windows/make_zip.sh OUT.zip
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 1 ] || { echo "usage: make_zip.sh OUT.zip   (voices are never bundled)" >&2; exit 2; }
out=$(realpath -m "$1")

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
git archive --format=tar --prefix=Hankify/ HEAD | tar -x -C "$stage"
# guard: refuse to build if any model file slipped into the repo
bad=$(cd "$stage" && find Hankify -type f \( -iname '*.pth' -o -iname '*.index' -o -iname '*.onnx' -o -iname '*.ckpt' -o -iname '*.pt' \))
if [ -n "$bad" ]; then echo "refusing: model files in the package:" >&2; echo "$bad" >&2; exit 1; fi
rm -f "$out"
(cd "$stage" && zip -qr -X "$out" Hankify)
echo "$out ($(du -h "$out" | cut -f1)), no voices"
