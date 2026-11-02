#!/usr/bin/env python3
"""Install RVC voices from what people actually download: a .zip (as voice-models.com and most other sites
hand them out), or a loose .pth with an optional .index next to it.

Each voice's .pth goes to the RVC weights folder and its index to the indices folder as "<voice>.index", the
exact name rvc_stage.py looks for first, so it can never be confused with another voice's index.
Training leftovers in the zips (G_*.pth / D_*.pth checkpoints, trained_*.index) are skipped.

    python voice_import.py "Some Voice.zip" [more.zip | voice.pth | voice.index ...]

Standard library only. Used by Hankify's "Import Voice..." button and by the Windows installer
(which runs it on everything in the voices folder).
"""
import filecmp
import os
import re
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hankify_config as cfg  # noqa: E402

TRAINING_CKPT = re.compile(r"^[GD]_\d+\.pth$", re.IGNORECASE)
MAX_MEMBER = 2 * 1024 ** 3   # nothing in a voice zip is bigger than this; bail out on zip bombs


def _is_voice(name):
    base = os.path.basename(name)
    return base.lower().endswith(".pth") and not TRAINING_CKPT.match(base) and not base.startswith("._")


def _is_index(name):
    base = os.path.basename(name)
    return base.lower().endswith(".index") and "trained" not in base.lower() and not base.startswith("._")


def _clean_stem(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    stem = re.sub(r"_e\d+_s\d+$", "", stem, flags=re.IGNORECASE)   # drop RVC's "_e300_s9000" training suffix
    return stem or "Voice"


def _pair(voices, indexes):
    """Match each voice file with its index: by name if possible, else the only one there is."""
    pairs = {}
    free = list(indexes)
    for v in voices:
        exp = re.sub(r"_e\d+_s\d+$", "", _clean_stem(v), flags=re.IGNORECASE).lower()
        hits = [i for i in free if exp and exp in os.path.basename(i).lower()]
        if hits:
            # prefer the "added_" index RVC training produces for inference
            hits.sort(key=lambda i: (0 if "added" in os.path.basename(i).lower() else 1, i))
            pairs[v] = hits[0]
            free.remove(hits[0])
    single = [v for v in voices if v not in pairs]
    if len(single) == 1 and free:
        free.sort(key=lambda i: (0 if "added" in os.path.basename(i).lower() else 1, i))
        pairs[single[0]] = free[0]
    return pairs


def _place(src, dest_dir, stem, ext):
    """Copy src to dest_dir/stem+ext. Returns (final stem, "new"|"same"). A different file of the same
    name is never overwritten: the new one gets " (2)", " (3)"..."""
    os.makedirs(dest_dir, exist_ok=True)
    n = 1
    while True:
        name = stem if n == 1 else f"{stem} ({n})"
        dest = os.path.join(dest_dir, name + ext)
        if not os.path.exists(dest):
            shutil.copyfile(src, dest)
            return name, "new"
        if filecmp.cmp(src, dest, shallow=False):
            return name, "same"
        n += 1


GENERIC = {"model", "voice", "weights", "final", "best", "rvc", "g", "net_g"}


def _install(voice_src, index_src, weights_dir, indices_dir, fallback_name=None):
    stem = _clean_stem(voice_src)
    if fallback_name and stem.lower() in GENERIC:
        stem = _clean_stem(fallback_name)   # "model.pth" inside "My Voice.zip" -> "My Voice"
    stem, state = _place(voice_src, weights_dir, stem, ".pth")
    has_index = False
    if index_src:
        dest = os.path.join(indices_dir, stem + ".index")
        if not os.path.exists(dest) or not filecmp.cmp(index_src, dest, shallow=False):
            os.makedirs(indices_dir, exist_ok=True)
            shutil.copyfile(index_src, dest)
        has_index = True
    else:
        has_index = os.path.exists(os.path.join(indices_dir, stem + ".index"))
    return {"voice": stem + ".pth", "status": "already installed" if state == "same" else "installed",
            "index": has_index}


def import_paths(paths, weights_dir=None, indices_dir=None):
    """Import every voice found in paths (.zip / .pth / .index files, or folders of them).
    Returns (results, problems): results = [{"voice", "status", "index"}], problems = [str]."""
    weights_dir = weights_dir or cfg.RVC_WEIGHTS_DIR
    indices_dir = indices_dir or cfg.RVC_INDICES_DIR
    results, problems = [], []
    loose_voices, loose_indexes = [], []

    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in sorted(names)]
        else:
            files.append(p)

    with tempfile.TemporaryDirectory(prefix="hankify-import-") as tmp:
        for f in files:
            low = f.lower()
            if low.endswith(".zip"):
                try:
                    with zipfile.ZipFile(f) as z:
                        members = [m for m in z.infolist() if not m.is_dir() and "__MACOSX" not in m.filename]
                        voices = [m for m in members if _is_voice(m.filename)]
                        indexes = [m for m in members if _is_index(m.filename)]
                        if not voices:
                            problems.append(f"{os.path.basename(f)}: no RVC voice (.pth) inside. It may be a "
                                            "different kind of model (Hankify only takes RVC models).")
                            continue
                        out = tempfile.mkdtemp(dir=tmp)
                        extracted = {}
                        for m in voices + indexes:
                            if m.file_size > MAX_MEMBER:
                                raise ValueError(f"{m.filename} is suspiciously large")
                            # write by base name only, so paths inside the zip can't escape the temp folder
                            sub = tempfile.mkdtemp(dir=out)
                            dest = os.path.join(sub, os.path.basename(m.filename))
                            with z.open(m) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extracted[m.filename] = dest
                except (zipfile.BadZipFile, OSError, ValueError) as e:
                    problems.append(f"{os.path.basename(f)}: couldn't open the zip ({e})")
                    continue
                v_paths = [extracted[m.filename] for m in voices]
                i_paths = [extracted[m.filename] for m in indexes]
                pairs = _pair(v_paths, i_paths)
                for v in v_paths:
                    results.append(_install(v, pairs.get(v), weights_dir, indices_dir,
                                            fallback_name=f if len(v_paths) == 1 else None))
            elif _is_voice(f):
                loose_voices.append(f)
            elif _is_index(f):
                loose_indexes.append(f)
            elif low.endswith(".pth"):
                problems.append(f"{os.path.basename(f)}: a training checkpoint, not a voice (skipped)")
            elif not low.endswith((".txt", ".md", ".index", ".npy", ".json", ".png", ".jpg")):
                problems.append(f"{os.path.basename(f)}: not a .zip, .pth or .index file (skipped)")

        pairs = _pair(loose_voices, loose_indexes)
        for v in loose_voices:
            results.append(_install(v, pairs.get(v), weights_dir, indices_dir))
        if loose_indexes and not loose_voices:
            problems.append("an .index file on its own does nothing: import it together with its .pth (or the zip)")
    return results, problems


def summary(results, problems):
    lines = []
    for r in results:
        lines.append(f"{r['status']}: {os.path.splitext(r['voice'])[0]}" + ("" if r["index"] else " (no .index)"))
    return lines + problems


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    res, probs = import_paths(sys.argv[1:])
    for line in summary(res, probs):
        print("    " + line)
    sys.exit(1 if probs and not res else 0)
