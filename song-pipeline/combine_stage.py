#!/usr/bin/env python3
"""Combine a converted vocal track with an instrumental track into a 320kbps MP3.

The converted vocal first goes through the EQ + compressor presets in
vocal_presets.json (next to this script), then is gained to match the
original vocal stem's loudness (when --reference-vocals is given).

Plain python3, no special venv needed - just calls ffmpeg.
"""
import argparse
import ctypes
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

PR_SET_PDEATHSIG = 1
WAVES_PY = os.path.expanduser("~/.local/share/vocal-rider-host/.venv/bin/python")
PRESETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocal_presets.json")


# Optional display names for the ID3 artist tag, keyed by voice model name (filename without .pth), e.g.
#   "artist_names": {"My Voice v2 (300 epochs)": "My Voice"}   in the Hankifier config.json.
# A model with no entry is tagged with its own name.
import hankify_config as _cfg  # noqa: E402
ARTIST_NAMES = _cfg.get("artist_names") or {}

def _die_with_parent():
    """preexec_fn: SIGKILL ffmpeg the instant this script's parent dies."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


if os.name == "nt":       # no prctl on Windows (and preexec_fn isn't supported there)
    _die_with_parent = None


def fail(message):
    print(f"combine_stage: {message}", file=sys.stderr)
    sys.exit(1)


def integrated_lufs(path):
    """Gated integrated loudness (EBU R128) of a file, or None if unmeasurable."""
    result = subprocess.run(
        ["ffmpeg", "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_die_with_parent,
    )
    m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", result.stdout)
    return float(m[-1]) if m else None


def fx_chain(p):
    """ffmpeg filter string for the vocal_presets.json "chain" stages, in order."""
    f = []
    for st in p.get("chain", []):
        t = st["type"]
        if t == "highpass":
            f.append(f"highpass=f={st['freq_hz']}:poles=2")
        elif t == "eq":
            f.append(f"equalizer=f={st['freq_hz']}:t=q:w={st.get('q', 1.0)}:g={st['gain_db']}")
        elif t == "highshelf":
            f.append(f"highshelf=f={st['freq_hz']}:g={st['gain_db']}")
        elif t == "compressor":
            f.append("acompressor=threshold={t}dB:ratio={r}:attack={a}:release={rl}:knee={k}dB:makeup={m}dB".format(
                t=st.get("threshold_db", -24), r=st.get("ratio", 2), a=st.get("attack_ms", 10),
                rl=st.get("release_ms", 50), k=st.get("knee_db", 2.83), m=st.get("makeup_db", 0)))
        elif t == "deesser":
            f.append(f"deesser=i={st.get('intensity', 0.5)}:m={st.get('max_reduction', 0.5)}:f={st.get('freq', 0.5)}")
        else:
            fail(f"unknown vocal_presets.json chain stage type: {t}")
    return ",".join(f)


def make_reverb(vocals, tmp_dir, rv):
    """Convolve the (mono) vocal with a generated stereo short-tail IR: decaying
    pink noise, a different seed per channel so the tail is wide. Returns the
    wet-only stereo wav."""
    rate = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                           "stream=sample_rate", "-of", "csv=p=0", vocals],
                          stdout=subprocess.PIPE, text=True).stdout.strip() or "48000"
    d = rv.get("decay_s", 0.8)
    pre = rv.get("predelay_ms", 18)
    ir = os.path.join(tmp_dir, "reverb_ir.wav")
    wet = os.path.join(tmp_dir, "vocal_reverb.wav")
    noise = "anoisesrc=d={d}:c=pink:r={r}:a=0.5:seed={s}"
    for cmd in (
        ["ffmpeg", "-y", "-f", "lavfi", "-i", noise.format(d=d, r=rate, s=11),
         "-f", "lavfi", "-i", noise.format(d=d, r=rate, s=29), "-filter_complex",
         f"[0][1]join=inputs=2:channel_layout=stereo,afade=t=out:st=0:d={d}:curve=exp,"
         f"adelay={pre}|{pre},highpass=f={rv.get('highpass_hz', 250)},lowpass=f={rv.get('lowpass_hz', 6500)}",
         ir],
        # afir: dry = input gain, wet = output gain (dry=0 would mute it)
        ["ffmpeg", "-y", "-i", vocals, "-i", ir, "-filter_complex",
         "[0:a]pan=stereo|c0=c0|c1=c0[s];[s][1:a]afir=dry=1:wet=1", "-c:a", "pcm_f32le", wet],
    ):
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           preexec_fn=_die_with_parent)
        if r.returncode != 0:
            fail(f"reverb ffmpeg failed:\n{r.stdout[-3000:]}")
    return wet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocals", required=True, help="Converted vocal track")
    parser.add_argument("--instrumental", required=True, help="Original instrumental track")
    parser.add_argument("--backing-vocals", help="Original, unconverted backing-vocals track (optional)")
    parser.add_argument("--original", help="the original song (new way: tonal-match reference)")
    parser.add_argument("--stereo-extra",
                        help="Stereo track summed into the mix as-is (no fx, not a tonal-match reference), e.g. the "
                             "original backing vocals' side signal so a mono vocal keeps the song's width")
    parser.add_argument("--reference-vocals",
                        help="Original, unconverted vocal stem; the converted vocal is gained to match its loudness")
    parser.add_argument("--vocal-gain-db", type=float, default=0.0,
                        help="Extra manual gain (dB) on the converted vocal, on top of the loudness match")
    parser.add_argument("--vocal-boost-db", type=float,
                        help="Exact gain (dB) on the processed vocal, instead of matching the original's loudness")
    parser.add_argument("--vocal-reverb", type=float, metavar="PERCENT",
                        help="Short-tail reverb on the converted vocal at PERCENT of the dry level "
                             "(e.g. 10; amplitude ratio, so 10 = 20 dB under the dry vocal). 0 = off. "
                             "Omitted = vocal_presets.json reverb default (on at default_percent)")
    parser.add_argument("--no-vocal-rider", action="store_true", help="Skip the vocal rider even though it is on by default.")
    parser.add_argument("--vocal-rider", action="store_true",
                        help="Even out the converted vocal's phrase-level loudness first (vocal_rider.py, a native "
                             "stand-in for Waves Vocal Rider; settings under \"rider\" in vocal_presets.json). Off by default.")
    parser.add_argument("--no-vocal-fx", action="store_true",
                        help="Skip the vocal_presets.json EQ/compressor/loudness processing (old behavior)")
    parser.add_argument("--presets", default=PRESETS, metavar="JSON_FILE",
                        help="Vocal FX presets file (default: vocal_presets.json next to this script); "
                             "per-song EQ variants without touching the house default")
    parser.add_argument("--no-tonal-match", action="store_true",
                        help="Skip the master tonal-balance match to the original song (presets tonal_match)")
    parser.add_argument("--no-loudnorm", action="store_true",
                        help="Skip the final two-pass loudness normalisation (presets master_loudness)")
    parser.add_argument("--output", required=True, help="Path to write the final MP3 to")
    parser.add_argument("--artist", help="ID3 artist tag to write (the RVC voice used)")
    parser.add_argument("--original-artist", help="ID3 Original Artist (TOPE) tag: the song's real artist")
    parser.add_argument("--title", help="ID3 title tag to write (inherited from the original song)")
    args = parser.parse_args()

    # NEW WAY is the default (2026-10-23): hand off to combine_new.py when the presets say so.
    # COMBINE_WAY=old forces this file's old chain; --no-vocal-fx / --no-loudnorm / no reference also stay old.
    try:
        mode = json.load(open(args.presets)).get("mode", "old")
    except (OSError, ValueError):
        mode = "old"
    if (mode == "new" and os.environ.get("COMBINE_WAY") != "old" and not args.no_vocal_fx and not args.no_loudnorm
            and args.reference_vocals and os.path.isfile(args.reference_vocals)):
        import hankify_config  # noqa: E402
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [hankify_config.RVC_PYTHON, os.path.join(here, "combine_new.py"), "--vocals", args.vocals,
               "--instrumental", args.instrumental, "--reference-vocals", args.reference_vocals, "--output", args.output,
               "--space", str(10.0 if args.vocal_reverb is None else args.vocal_reverb),
               "--vocal-gain-db", str(args.vocal_gain_db + (args.vocal_boost_db or 0.0))]
        for flag, val in (("--backing", args.backing_vocals), ("--original", args.original), ("--stereo-extra", args.stereo_extra),
                          ("--artist", args.artist), ("--title", args.title), ("--original-artist", args.original_artist)):
            if val:
                cmd += [flag, val]
        print("combine_stage: new way -> combine_new.py", file=sys.stderr)
        sys.exit(subprocess.run(cmd, preexec_fn=_die_with_parent).returncode)

    vocals = os.path.abspath(args.vocals)
    instrumental = os.path.abspath(args.instrumental)
    backing_vocals = os.path.abspath(args.backing_vocals) if args.backing_vocals else None
    output = os.path.abspath(args.output)

    if not os.path.isfile(vocals):
        fail(f"vocals file not found: {vocals}")
    if not os.path.isfile(instrumental):
        fail(f"instrumental file not found: {instrumental}")
    if backing_vocals and not os.path.isfile(backing_vocals):
        fail(f"backing vocals file not found: {backing_vocals}")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    presets = {}
    if not args.no_vocal_fx and os.path.isfile(args.presets):
        with open(args.presets) as fh:
            presets = json.load(fh)
    loud = presets.get("loudness", {})

    # Optional fader riding (before the EQ/compression chain, like Vocal Rider sits first on a vocal channel).
    tmp_dir = None
    rider = presets.get("rider", {})
    if (args.vocal_rider or rider.get("enabled")) and not args.no_vocal_rider:
        import hankify_config  # noqa: E402  (stdlib only; gives the RVC venv python that has numpy/soundfile)
        here = os.path.dirname(os.path.abspath(__file__))
        tmp_dir = tempfile.mkdtemp(prefix="vocalfx_", dir=os.path.dirname(output) or None)
        ridden = os.path.join(tmp_dir, "vocal_ridden.wav")
        done = False
        if rider.get("engine", "waves") == "waves" and os.path.isfile(WAVES_PY):
            try:   # the Waves host can hang on near-silent input (2026-10-23): fall back instead of dying
                r = subprocess.run([WAVES_PY, os.path.join(here, "waves_rider.py"), vocals, ridden,
                                    "--range", str(rider.get("range_db", 6)), "--attack", rider.get("attack", "Slow")],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_die_with_parent,
                                   timeout=600)
            except subprocess.TimeoutExpired:
                r = subprocess.CompletedProcess([], 1, "Waves Vocal Rider timed out after 600 s")
            if r.returncode == 0 and os.path.isfile(ridden) and os.path.getsize(ridden) > 1000:
                print("combine_stage: " + r.stdout.strip().splitlines()[-1], file=sys.stderr)
                done = True
            else:
                print(f"combine_stage: Waves Vocal Rider failed, using native rider:\n{r.stdout[-800:]}", file=sys.stderr)
        if not done:
            r = subprocess.run([hankify_config.RVC_PYTHON, os.path.join(here, "vocal_rider.py"),
                                vocals, ridden, "--up", str(rider.get("up_db", 6)), "--down", str(rider.get("down_db", 8)),
                                "--strength", str(rider.get("strength", 1.0))],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_die_with_parent)
            if r.returncode != 0:
                fail(f"vocal rider failed:\n{r.stdout[-3000:]}")
            print("combine_stage: " + r.stdout.strip(), file=sys.stderr)
        vocals = ridden

    # EQ + compress the converted vocal into a temp wav first, so the
    # loudness match below measures the processed vocal, not the raw one.
    chain = fx_chain(presets) if presets else ""
    if chain:
        tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="vocalfx_", dir=os.path.dirname(output) or None)
        processed = os.path.join(tmp_dir, "vocal_fx.wav")
        r = subprocess.run(["ffmpeg", "-y", "-i", vocals, "-af", chain, "-c:a", "pcm_f32le", processed],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           preexec_fn=_die_with_parent)
        if r.returncode != 0:
            fail(f"vocal fx ffmpeg failed:\n{r.stdout[-3000:]}")
        print(f"combine_stage: vocal fx: {chain}", file=sys.stderr)
        vocals = processed

    try:
        # RVC's output is always mono (it synthesizes a single voice track);
        # the other tracks are normally stereo. Center the mono vocal into both
        # channels first so their stereo image survives the mix - otherwise amix
        # silently downmixes everything to mono.
        # normalize=0: amix defaults to averaging (and thus quieting) its
        # inputs to avoid clipping. These tracks were separated from one source
        # and should be summed back at full gain instead, matching what a
        # straight Audacity track mixdown does.
        gain_db = args.vocal_gain_db
        match_on = loud.get("match_original", True) if presets else True
        if args.vocal_boost_db is not None:
            print(f"combine_stage: vocal boost {args.vocal_boost_db:+.1f} dB (per-song)", file=sys.stderr)
            gain_db += args.vocal_boost_db
        elif args.reference_vocals and os.path.isfile(args.reference_vocals) and match_on:
            ref_lufs = integrated_lufs(args.reference_vocals)
            conv_lufs = integrated_lufs(vocals)
            if ref_lufs is not None and conv_lufs is not None:
                offset = loud.get("offset_db", 0.0)
                match = max(-6.0, min(loud.get("max_boost_db", 18.0), ref_lufs - conv_lufs + offset))
                print(f"combine_stage: original vocal {ref_lufs:.1f} LUFS, converted {conv_lufs:.1f} LUFS "
                      f"-> gain {match:+.1f} dB", file=sys.stderr)
                gain_db += match
        # Reverb: rendered wet-only, then gained so it sits PERCENT (amplitude)
        # under the dry vocal no matter how loud the IR/convolution comes out.
        wet_chain = None
        rv = presets.get("reverb", {})
        if args.vocal_reverb is None and rv.get("default_on"):
            args.vocal_reverb = float(rv.get("default_percent", 8))
        if args.vocal_reverb:
            if not tmp_dir:
                tmp_dir = tempfile.mkdtemp(prefix="vocalfx_", dir=os.path.dirname(output) or None)
            wet = make_reverb(vocals, tmp_dir, presets.get("reverb", {}))
            dry_lufs = integrated_lufs(vocals)
            wet_lufs = integrated_lufs(wet)
            if dry_lufs is not None and wet_lufs is not None:
                # mono measured vs stereo: the dry vocal is panned to both channels (+3 dB) in the mix
                wet_db = gain_db + (dry_lufs + 3.0 - wet_lufs) + 20 * math.log10(args.vocal_reverb / 100.0)
                wet_chain = f"volume={wet_db:.2f}dB"
                print(f"combine_stage: reverb {args.vocal_reverb:g}% -> wet at {wet_db:+.1f} dB", file=sys.stderr)

        vox_chain = "pan=stereo|c0=c0|c1=c0"
        if gain_db:
            vox_chain += f",volume={gain_db:.2f}dB,alimiter=limit=0.97"

        # Summing at full gain can push the mix over 0 dBFS (seen even before the
        # vocal fx), so a brickwall limiter on the master keeps the MP3 from clipping.
        master = "alimiter=limit=0.95:level=disabled" if presets else "anull"

        inputs = ["-i", vocals, "-i", instrumental]
        mix_in = ["[vox_stereo]", "[1:a]"]
        if backing_vocals:
            inputs += ["-i", backing_vocals]
            mix_in.append(f"[{len(inputs) // 2 - 1}:a]")
        if args.stereo_extra:
            inputs += ["-i", os.path.abspath(args.stereo_extra)]
            mix_in.append(f"[{len(inputs) // 2 - 1}:a]")
        pre = f"[0:a]{vox_chain}[vox_stereo];"
        if wet_chain:
            inputs += ["-i", wet]
            pre += f"[{len(inputs) // 2 - 1}:a]{wet_chain}[wet];"
            mix_in.append("[wet]")
        filter_complex = (
            pre + "".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=longest:"
            "dropout_transition=0:normalize=0,"
            f"{master}[out]"
        )
        metadata = []
        if args.artist:
            artist = " & ".join(ARTIST_NAMES.get(a, a) for a in args.artist.split(" & "))
            metadata += ["-metadata", f"artist={artist}"]
        if args.original_artist:
            metadata += ["-metadata", f"TOPE={args.original_artist}"]
        if args.title:
            metadata += ["-metadata", f"title={args.title}"]
        # Final master loudness (user 2026-10-08: "the standard for youtube"): two-pass EBU R128 loudnorm to
        # presets master_loudness (default -14 LUFS integrated, -1 dBTP) so every render plays at the same level.
        ml = presets.get("master_loudness", {})
        do_norm = bool(ml.get("enabled")) and not args.no_loudnorm
        if do_norm:
            if not tmp_dir:
                tmp_dir = tempfile.mkdtemp(prefix="vocalfx_", dir=os.path.dirname(output) or None)
            mix_target = os.path.join(tmp_dir, "mix.wav")
            enc = ["-c:a", "pcm_f32le"]
        else:
            mix_target, enc = output, ["-c:a", "libmp3lame", "-b:a", "320k", *metadata]
        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            *enc,
            mix_target,
        ]
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_die_with_parent
        )
        if result.returncode != 0:
            fail(f"ffmpeg failed:\n{result.stdout}")
        # Tonal-balance match (2026-10-22, audio-engineering advice: "follow the pink-noise curve", every song):
        # gentle firequalizer nudging the mix's third-octave shape toward the original song's own stems.
        tone = "anull"
        tm = presets.get("tonal_match", {})
        if (do_norm and tm.get("enabled") and not args.no_tonal_match and args.reference_vocals
                and os.path.isfile(args.reference_vocals)):
            import hankify_config  # noqa: E402  (RVC venv python has numpy/soundfile)
            refs = [instrumental, args.reference_vocals] + ([backing_vocals] if backing_vocals else [])
            r = subprocess.run([hankify_config.RVC_PYTHON, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tonal_match.py"), mix_target, *refs,
                                "--max-cut", str(tm.get("max_cut_db", 4.0)), "--max-boost", str(tm.get("max_boost_db", 2.0)),
                                "--no-boost-above", str(tm.get("no_boost_above_hz", 2500))],
                               capture_output=True, text=True, preexec_fn=_die_with_parent)
            if r.returncode == 0 and r.stdout.strip():
                tone = r.stdout.strip().splitlines()[-1]
                print("combine_stage: " + r.stderr.strip().splitlines()[-1], file=sys.stderr)
            else:
                print(f"combine_stage: tonal match failed, skipped:\n{r.stderr[-800:]}", file=sys.stderr)
        if do_norm:
            I, TP, LRA = ml.get("target_lufs", -14.0), ml.get("true_peak_db", -1.0), ml.get("lra", 11.0)
            sr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
                                 "-of", "csv=p=0", instrumental], capture_output=True, text=True).stdout.strip() or "44100"
            m = subprocess.run(["ffmpeg", "-nostats", "-i", mix_target, "-af",
                                f"{tone},loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json", "-f", "null", "-"],
                               capture_output=True, text=True, preexec_fn=_die_with_parent).stderr
            j = json.loads(m[m.rindex("{"):m.rindex("}") + 1])
            ln = (f"{tone},loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={j['input_i']}:measured_TP={j['input_tp']}:"
                  f"measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true")
            r2 = subprocess.run(["ffmpeg", "-y", "-i", mix_target, "-af", ln, "-ar", sr, "-c:a", "libmp3lame", "-b:a", "320k",
                                 *metadata, output], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                preexec_fn=_die_with_parent)
            if r2.returncode != 0:
                fail(f"loudnorm ffmpeg failed:\n{r2.stdout[-3000:]}")
            print(f"combine_stage: master loudness {float(j['input_i']):.1f} LUFS -> {I} LUFS (TP {TP} dB)", file=sys.stderr)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print(output)


if __name__ == "__main__":
    main()
