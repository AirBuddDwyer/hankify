"""CI only: run one small Hankiplier job headlessly (no window) and check the result.

Builds a two-singer project the way the Hankiplier window would, then runs the engine's two steps:
  1. convert the vocal with each singer's voice (rvc_stage.py, the same converter the song pipeline uses)
  2. engine.py analyze  -> Whisper word timing + who-sings-what cuts (plan.json)
  3. engine.py build    -> splice the voices at the cuts, mix with the instrumental (one MP3)
Separation is skipped on purpose (the vocal and instrumental are given as stems): the plain Hankify test
already covers it, and this keeps the job short on a CPU runner.

    python hankiplier_job.py --vocals V.wav --instrumental I.wav --lyrics L.txt --default "Singer A" \
        --cast "Singer A=Voice A.pth" --cast "Singer B=Voice B.pth" --work DIR --out OUT.mp3

Environment: HANKIFIER_CONFIG as usual; HANKIPLIER_WHISPER_MODEL=tiny keeps the transcription fast.
Exits non-zero (with the reason) if any step fails or the result looks wrong.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "hankiplier"))
import hk_paths  # noqa: E402
import lyrics as lyr  # noqa: E402

cfg = hk_paths.cfg
ENGINE = os.path.join(ROOT, "hankiplier", "engine.py")


def run(cmd, label):
    print(f"--> {label}", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"FAILED: {label} (exit {r.returncode})")


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocals", required=True)
    ap.add_argument("--instrumental", required=True)
    ap.add_argument("--lyrics", required=True)
    ap.add_argument("--default", required=True, help="performer for lines with no header")
    ap.add_argument("--cast", action="append", required=True, help='"Performer=Voice.pth", once per singer')
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    work = os.path.abspath(a.work)
    pdir = os.path.join(work, "project")
    os.makedirs(pdir, exist_ok=True)
    vocals, inst = os.path.abspath(a.vocals), os.path.abspath(a.instrumental)
    song = os.path.join(work, "song.wav")      # the "original" the mixer tone-matches against
    run(["ffmpeg", "-v", "error", "-y", "-i", vocals, "-i", inst, "-filter_complex", "amix=inputs=2:normalize=0", song],
        "mix the test song")

    cast = dict(c.split("=", 1) for c in a.cast)
    text = open(a.lyrics, encoding="utf-8").read()
    parts = lyr.parse_parts(text, a.default)
    missing = [p for p in parts["performers"] if p not in cast]
    if missing:
        sys.exit(f"FAILED: no voice cast for {missing}")

    renders = {}
    for perf, voice in cast.items():
        conv = os.path.join(work, f"{os.path.splitext(voice)[0]}_(Converted Vocals).wav")
        if not os.path.isfile(conv):
            run([cfg.RVC_PYTHON, os.path.join(hk_paths.PIPELINE_DIR, "rvc_stage.py"), "--input", vocals,
                 "--model", voice, "--output", conv, "--index-rate", "0.75"], f"convert the vocal as {voice}")
        renders[f"{voice}|0"] = conv

    proj = {"song": song, "name": "CI multi-voice", "title": "CI multi-voice", "lyrics_text": text, "parts": parts,
            "parts_hash": hashlib.sha1(json.dumps(parts["lines"], sort_keys=True).encode()).hexdigest(),
            "casting": {p: {"voice": v, "pitch": 0, "backing": False} for p, v in cast.items()},
            "renders": renders, "stems": {"vocals": vocals, "instrumental": inst},
            "output_path": os.path.abspath(a.out)}
    with open(os.path.join(pdir, "project.json"), "w", encoding="utf-8") as f:
        json.dump(proj, f, indent=1)

    run([cfg.RVC_PYTHON, ENGINE, "analyze", pdir], "engine analyze (Whisper + cuts)")
    plan = json.load(open(os.path.join(pdir, "plan.json"), encoding="utf-8"))
    secs = plan["sections"]
    sung = {p for s in secs for p in s["performers"]}
    print(f"    {len(secs)} sections, performers: {sorted(sung)}")
    for s in secs:
        print(f"      {s['start']:7.2f}  {' + '.join(s['performers']):12s} {s['lyric'][:50]}")
    if len(secs) < 2 or not set(cast) <= sung:
        sys.exit("FAILED: the plan should have cuts for every singer")

    run([cfg.RVC_PYTHON, ENGINE, "build", pdir], "engine build (splice + mix)")
    got, want = duration(a.out), duration(song)
    print(f"    {os.path.basename(a.out)}: {got:.1f} s (song {want:.1f} s)")
    if abs(got - want) > 2:
        sys.exit("FAILED: the output has the wrong length")
    print("Hankiplier job OK")


if __name__ == "__main__":
    main()
