#!/usr/bin/env python3
"""End-to-end pipeline: UVR5 separation -> RVC voice conversion -> mix -> 320kbps MP3.

    python3 pipeline.py <song> --voice <model.pth> [--outdir DIR]   (paths: see hankify_config.py)

Each stage runs as its own subprocess, using that tool's own venv, and each
one fully exits before the next starts - this is what keeps UVR5 and
RVC-WebUI from ever fighting over the GPU at the same time. Before each
stage, it also stops any other known GPU-using process (a left-open UVR5
GUI, an RVC-WebUI server, or an orphaned uvr_stage.py/rvc_stage.py left
behind by some earlier, non-gracefully-terminated run) so a stage always
gets the whole card to itself.
"""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """preexec_fn: ask the kernel to SIGKILL this child the instant its
    direct parent dies, for any reason (clean exit, SIGTERM, SIGKILL, crash).
    Applied at every subprocess-spawn point in this whole pipeline (here and
    in each stage script), so killing Hankify - or this script - by any
    means cascades all the way down instead of leaving an orphaned
    UVR5/RVC process squatting on the GPU."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


if os.name == "nt":       # no prctl on Windows (and preexec_fn isn't supported there)
    _die_with_parent = None


PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPELINE_DIR)
import hankify_config as cfg  # noqa: E402
UVR_PYTHON = cfg.UVR_PYTHON
RVC_PYTHON = cfg.RVC_PYTHON
DEFAULT_OUTDIR = cfg.OUTPUT_DIR
RUNS_DIR = cfg.RUNS_DIR

# Full-command-line patterns for other processes known to hold GPU memory on
# this machine - an interactive UVR5 GUI left open, an RVC-WebUI server, or
# this pipeline's own headless stage scripts if a *previous, non-gracefully-
# terminated* run left one orphaned and still holding a model in VRAM. This
# run's own stage subprocesses are never live when free_gpu() runs (each
# run_stage() call blocks until its subprocess exits, and free_gpu() only
# ever runs between stages), so matching on these script names too is safe -
# any live match is by definition a leftover from somewhere else.
GPU_COMPETING_PATTERNS = ["UVR.py", "webui.py", "uvr_stage.py", "rvc_stage.py"]


def fail(message):
    print(f"pipeline: {message}", file=sys.stderr)
    sys.exit(1)


def free_gpu():
    """Stop any other known GPU-using app so a pipeline stage gets the whole card."""
    if os.name == "nt":   # no pgrep; the Windows install only ever runs one stage at a time anyway
        return
    my_pid = os.getpid()
    for pattern in GPU_COMPETING_PATTERNS:
        result = subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.PIPE, text=True)
        pids = [int(p) for p in result.stdout.split() if int(p) != my_pid]
        if not pids:
            continue
        print(f"Stopping {pattern} (pid {', '.join(map(str, pids))}) to free the GPU...", flush=True)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(3)
        for pid in pids:
            try:
                os.kill(pid, 0)  # still alive?
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(1)


def run_stage(label, cmd):
    print(f"{label} ...", flush=True)
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=_die_with_parent
    )
    if result.returncode != 0:
        fail(f"{label} failed:\n{result.stderr}")
    return result.stdout.strip()


def _read_title_tag(path, tag="title"):
    """Read the original song's existing ID3 title tag (or another tag, e.g.
    artist), if any, so the output MP3 can inherit it instead of falling back
    to the filename."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", f"format_tags={tag}",
         "-of", "default=nw=1:nk=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    title = result.stdout.strip()
    return title or None


def _hash_file(path, chunk_size=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_cached_separation(intermediates_dir, file_hash, split_backing_vocals):
    """Look for a previous run's UVR5 output for this exact input file (by
    content hash, so a rename/copy still matches) under intermediates_dir.
    A cached run only counts if it has the stems this run needs - a plain
    Vocals/Instrumental split from before won't satisfy a request that also
    needs separate lead/backing vocal stems, and vice versa. Returns a stems
    dict (see stage1_out parsing in main()) or None."""
    if not intermediates_dir or not file_hash or not os.path.isdir(intermediates_dir):
        return None
    try:
        entries = sorted(os.listdir(intermediates_dir))
    except OSError:
        return None
    for name in entries:
        subdir = os.path.join(intermediates_dir, name)
        sidecar = os.path.join(subdir, "source.json")
        try:
            with open(sidecar) as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if info.get("sha256") != file_hash:
            continue
        try:
            names = os.listdir(subdir)
        except OSError:
            continue
        instrumental = [f for f in names if f.endswith("(Instrumental).wav")]
        if len(instrumental) != 1:
            continue
        if split_backing_vocals:
            lead = [f for f in names if f.endswith("(Vocals_lead_only).wav")]
            backing = [f for f in names if f.endswith("(Vocals_backing_only).wav")]
            if len(lead) == 1 and len(backing) == 1:
                return {
                    "lead_vocals": os.path.join(subdir, lead[0]),
                    "backing_vocals": os.path.join(subdir, backing[0]),
                    "instrumental": os.path.join(subdir, instrumental[0]),
                }
        else:
            vocals = [f for f in names if f.endswith("(Vocals).wav")]
            if len(vocals) == 1:
                return {
                    "vocals": os.path.join(subdir, vocals[0]),
                    "instrumental": os.path.join(subdir, instrumental[0]),
                }
    return None


def _dispose_of_intermediates(run_dir, out_name, keep_dir, file_hash):
    """Move run_dir's contents into keep_dir/<out_name> if the caller asked
    to keep intermediates, otherwise delete them. Runs in a finally block so
    it happens whether the pipeline succeeded or failed partway through -
    without it, the runs folder grows without bound (it hit 9GB+
    across ~100 runs before this existed). A source.json sidecar records the
    input's hash so a later run on the same song can find and reuse this
    separation instead of redoing it (see _find_cached_separation)."""
    if not os.path.isdir(run_dir):
        return
    if not keep_dir:
        shutil.rmtree(run_dir, ignore_errors=True)
        return

    dest = os.path.join(keep_dir, out_name)
    final_dest = dest
    n = 1
    while os.path.exists(final_dest):
        final_dest = f"{dest} ({n})"
        n += 1
    try:
        os.makedirs(keep_dir, exist_ok=True)
        shutil.move(run_dir, final_dest)
        if file_hash:
            try:
                with open(os.path.join(final_dest, "source.json"), "w") as f:
                    json.dump({"sha256": file_hash}, f)
            except OSError as e:
                print(f"pipeline: couldn't write source.json: {e}", file=sys.stderr)
        print(f"Intermediate files kept in: {final_dest}")
    except OSError as e:
        print(f"pipeline: couldn't move intermediates to {final_dest}: {e}", file=sys.stderr)


def _fmt_time(seconds):
    return f"{int(seconds // 60)}:{seconds % 60:05.2f}"


def _load_sections(path, default_pitch):
    """Read and validate a --sections file (see its --help)."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        fail(f"couldn't read --sections file: {e}")
    if not isinstance(raw, list) or len(raw) < 2:
        fail("--sections needs a list of at least two sections")
    sections = []
    for s in raw:
        try:
            sections.append({"start": float(s["start"]), "voice": str(s["voice"]),
                             "pitch": int(s.get("pitch", default_pitch))})
        except (KeyError, TypeError, ValueError):
            fail(f"bad section in --sections: {s!r}")
    if sections[0]["start"] != 0:
        fail("the first section must start at 0")
    if any(b["start"] <= a["start"] for a, b in zip(sections, sections[1:])):
        fail("section start times must be in increasing order")
    return sections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("song", help="Path to the input song")
    parser.add_argument("--voice", help="RVC model filename, e.g. My Voice.pth (required unless --sections is given)")
    parser.add_argument("--sections", metavar="JSON_FILE",
                         help="Multi-voice mode: JSON list of {\"start\": seconds, \"voice\": model.pth, "
                              "\"pitch\": semitones (optional, defaults to --f0-up-key)}, the first "
                              "starting at 0. The vocal is converted once per voice and spliced at "
                              "each start time (snapped to the nearest quiet gap within 0.5s)")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR, help=f"Where the final MP3 goes (default {DEFAULT_OUTDIR})")
    parser.add_argument("--out-name", help="Base filename (no extension) for the output MP3; defaults to the input song's basename")
    parser.add_argument("--intermediates-dir", metavar="DIR",
                         help="Directory to look in for a previous UVR5 separation of this exact "
                              "input file to reuse (skips re-running UVR5 if one is found), and, "
                              "with --keep-intermediates, to store this run's own into")
    parser.add_argument("--keep-intermediates", action="store_true",
                         help="Move intermediate files (stems, converted vocal) into "
                              "--intermediates-dir/<out-name> instead of deleting them once the "
                              "run finishes")
    parser.add_argument("--split-backing-vocals", action="store_true",
                         help="Convert only the lead vocal (via a UVR5 karaoke-split model) and mix "
                              "the original, unconverted backing vocals back in untouched")
    parser.add_argument("--skip-stemming", action="store_true",
                         help="Treat <song> as an already-isolated vocal stem instead of a full "
                              "song: skip UVR5 separation entirely and skip the final instrumental "
                              "mix, just writing the converted vocal out on its own")
    parser.add_argument("--match-vocal-loudness", action="store_true",
                        help="(Now always on, via vocal_presets.json) Gain the converted vocal to match "
                             "the loudness of the original vocal stem before mixing")
    parser.add_argument("--vocal-boost-db", type=float,
                        help="Exact gain (dB) on the converted vocal for this song, instead of the default "
                             "match-the-original-vocal loudness")
    parser.add_argument("--vocal-gain-db", type=float,
                        help="Extra gain (dB) on the converted vocal ON TOP of the auto loudness match "
                             "(e.g. -3 = a bit quieter than the original singer's level)")
    parser.add_argument("--vocal-reverb", type=float, metavar="PERCENT",
                        help="Short-tail reverb on the converted vocal at PERCENT of the dry level (e.g. 10); "
                             "0 = off; omitted = vocal_presets.json default (on, 8%%)")
    parser.add_argument("--no-vocal-fx", action="store_true",
                        help="Skip the vocal_presets.json EQ/compressor/loudness match on the converted vocal")
    parser.add_argument("--f0-up-key", type=int, default=0, help="Pitch shift in semitones, e.g. 6 or -6")
    parser.add_argument("--f0-method", default="rmvpe", choices=["pm", "rmvpe", "fcpe"])
    parser.add_argument("--index-rate", type=float, default=0.75)
    parser.add_argument("--rms-mix-rate", type=float, default=0.25)
    parser.add_argument("--protect", type=float, default=0.33)
    args = parser.parse_args()

    if args.skip_stemming and args.split_backing_vocals:
        fail("--skip-stemming and --split-backing-vocals can't be combined: there's no final "
             "mix step left for backing vocals to be preserved into")

    sections = _load_sections(args.sections, args.f0_up_key) if args.sections else None
    if not sections and not args.voice:
        fail("--voice is required (or give --sections for multi-voice)")

    song = os.path.abspath(args.song)
    if not os.path.isfile(song):
        fail(f"song not found: {song}")

    song_base = os.path.splitext(os.path.basename(song))[0]
    out_name = args.out_name or song_base
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(RUNS_DIR, f"{stamp}-{out_name}")
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)

    file_hash = _hash_file(song) if (args.intermediates_dir and not args.skip_stemming) else None
    cached = _find_cached_separation(args.intermediates_dir, file_hash, args.split_backing_vocals)

    try:
        free_gpu()

        if args.skip_stemming:
            print(f"[1/2] Skipping stemming (input is already a vocal stem): {song}", flush=True)
            vocal_to_convert_path = song
            instrumental_path = None
            backing_vocals_path = None
        elif cached:
            print(f"[1/3] Reusing cached separation (skipping UVR5): {next(iter(cached.values()))}", flush=True)
            stems = {}
            for key, cached_path in cached.items():
                dest = os.path.join(run_dir, os.path.basename(cached_path))
                shutil.copy2(cached_path, dest)
                stems[key] = dest
        else:
            print(f"[1/3] Separating stems (UVR5)...", flush=True)
            uvr_cmd = [UVR_PYTHON, os.path.join(PIPELINE_DIR, "uvr_stage.py"),
                       "--input", song, "--outdir", run_dir]
            if args.split_backing_vocals:
                uvr_cmd.append("--split-backing-vocals")
            stage1_out = run_stage("UVR5 separation", uvr_cmd)
            try:
                stems = json.loads(stage1_out)
            except json.JSONDecodeError as e:
                fail(f"could not parse UVR5 stage output ({e}):\n{stage1_out}")

        if not args.skip_stemming:
            instrumental_path = stems["instrumental"]
            if args.split_backing_vocals:
                vocal_to_convert_path = stems["lead_vocals"]
                backing_vocals_path = stems["backing_vocals"]
                print(f"    lead vocals:    {vocal_to_convert_path}")
                print(f"    backing vocals: {backing_vocals_path}")
            else:
                vocal_to_convert_path = stems["vocals"]
                backing_vocals_path = None
                print(f"    vocals:       {vocal_to_convert_path}")
            print(f"    instrumental: {instrumental_path}")

        free_gpu()

        total_stages = 2 if args.skip_stemming else 3
        converted_vocal_path = os.path.join(run_dir, f"{out_name}_(Converted Vocals).wav")

        def convert(voice, pitch, output):
            run_stage(
                "RVC conversion",
                [RVC_PYTHON, os.path.join(PIPELINE_DIR, "rvc_stage.py"),
                 "--input", vocal_to_convert_path, "--model", voice,
                 "--output", output,
                 "--f0-up-key", str(pitch),
                 "--f0-method", args.f0_method,
                 "--index-rate", str(args.index_rate),
                 "--rms-mix-rate", str(args.rms_mix_rate),
                 "--protect", str(args.protect)],
            )

        if sections:
            # One conversion per distinct voice/pitch, then splice them
            # together at the section boundaries.
            takes = {}
            for s in sections:
                key = (s["voice"], s["pitch"])
                if key not in takes:
                    takes[key] = os.path.join(run_dir, f"{out_name}_(Converted Vocals {len(takes) + 1}).wav")
            for n, ((voice, pitch), path) in enumerate(takes.items(), 1):
                print(f"[{total_stages - 1}/{total_stages}] Converting voice {n}/{len(takes)} "
                      f"({voice}, pitch {pitch:+d}, RVC-WebUI)...", flush=True)
                convert(voice, pitch, path)
                free_gpu()
            splice_json = os.path.join(run_dir, "sections.json")
            with open(splice_json, "w") as f:
                json.dump([{"start": s["start"], "vocal": takes[(s["voice"], s["pitch"])]}
                           for s in sections], f)
            splice_out = run_stage(
                "Splicing voices",
                [RVC_PYTHON, os.path.join(PIPELINE_DIR, "splice_stage.py"),
                 "--original", vocal_to_convert_path, "--sections", splice_json,
                 "--output", converted_vocal_path],
            )
            try:
                cuts = json.loads(splice_out.splitlines()[-1])["cuts"]
            except (ValueError, KeyError, IndexError):
                cuts = []
            for s, t in zip(sections[1:], cuts):
                print(f"    cut at {_fmt_time(t)} (asked {_fmt_time(s['start'])}) -> "
                      f"{os.path.splitext(s['voice'])[0]}", flush=True)
        else:
            print(f"[{total_stages - 1}/{total_stages}] Converting voice ({args.voice}, RVC-WebUI)...", flush=True)
            convert(args.voice, args.f0_up_key, converted_vocal_path)
        print(f"    converted vocal: {converted_vocal_path}")

        if args.skip_stemming:
            final_output = os.path.join(args.outdir, f"{out_name} (Converted Vocal).wav")
            shutil.copy2(converted_vocal_path, final_output)
            print(f"\nDone: {final_output}")
        else:
            print(f"[3/3] Mixing and exporting MP3...", flush=True)
            final_mp3 = os.path.join(args.outdir, f"{out_name}.mp3")
            if sections:
                voices = list(dict.fromkeys(s["voice"] for s in sections))
            else:
                voices = [args.voice]
            artist = " & ".join(os.path.splitext(os.path.basename(v))[0] for v in voices)
            title = _read_title_tag(song) or song_base
            combine_cmd = [sys.executable, os.path.join(PIPELINE_DIR, "combine_stage.py"),
                           "--vocals", converted_vocal_path,
                           "--instrumental", instrumental_path,
                           "--output", final_mp3,
                           "--artist", artist,
                           "--title", title]
            original_artist = _read_title_tag(song, "artist")
            if original_artist:
                combine_cmd += ["--original-artist", original_artist]
            if (args.match_vocal_loudness or not args.no_vocal_fx) and vocal_to_convert_path != song:
                combine_cmd += ["--reference-vocals", vocal_to_convert_path]
            if args.no_vocal_fx:
                combine_cmd += ["--no-vocal-fx"]
            if args.vocal_boost_db is not None:
                combine_cmd += ["--vocal-boost-db", str(args.vocal_boost_db)]
            if args.vocal_gain_db is not None:
                combine_cmd += ["--vocal-gain-db", str(args.vocal_gain_db)]
            if args.vocal_reverb is not None:      # 0 = off; omitted = presets default
                combine_cmd += ["--vocal-reverb", str(args.vocal_reverb)]
            if backing_vocals_path:
                combine_cmd += ["--backing-vocals", backing_vocals_path]
            if not args.skip_stemming:           # new way: tonal match against the real master
                combine_cmd += ["--original", song]
            run_stage("Combine/export", combine_cmd)

            print(f"\nDone: {final_mp3}")
    finally:
        keep_dir = args.intermediates_dir if args.keep_intermediates else None
        _dispose_of_intermediates(run_dir, out_name, keep_dir, file_hash)


if __name__ == "__main__":
    main()
