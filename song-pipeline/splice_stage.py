#!/usr/bin/env python3
"""Splice several converted-vocal tracks (one per voice) into one, switching
voice at each section boundary. Runs under RVC-WebUI's venv (needs numpy,
soundfile, librosa).

    splice_stage.py --original <vocal stem> --sections <sections.json> --output <out.wav>

sections.json is a list of {"start": seconds, "vocal": converted_wav_path},
sorted by start, the first starting at 0. Every converted track was made
from the same --original stem, so they're sample-aligned apart from sample
rate - different RVC models output different rates (e.g. 40k vs 48k), so
everything is resampled to the first section's rate.

Each boundary is snapped to the quietest point in the original vocal within
--snap-window seconds of the requested time (weighted toward the requested
time itself), so a cut lands between words
rather than mid-syllable, then crossfaded (equal power) over --crossfade
seconds. The final cut times are printed as JSON on stdout.
"""
import argparse
import json
import sys

import librosa
import numpy as np
import soundfile as sf


def fail(message):
    print(f"splice_stage: {message}", file=sys.stderr)
    sys.exit(1)


def quietest_point(mono, sr, target, window, probe=0.02, step=0.005, penalty_db_per_s=30.0):
    """Time (s) of the quietest `probe`-long spot within +/- window of target,
    where every second of distance from target costs penalty_db_per_s - so a
    precisely-entered time stays put unless there's a clearly quieter gap
    right next to it, instead of jumping to a marginally quieter one a
    syllable away."""
    half = int(probe * sr) // 2
    best_t, best_e = target, None
    for t in np.arange(max(target - window, probe), target + window, step):
        i = int(t * sr)
        seg = mono[max(0, i - half):i + half]
        if len(seg) == 0:
            continue
        e = 20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-9) + penalty_db_per_s * abs(t - target)
        if best_e is None or e < best_e:
            best_t, best_e = float(t), e
    return best_t


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, help="Unconverted vocal stem (used to find quiet cut points)")
    parser.add_argument("--sections", required=True, help="JSON file: [{start, vocal}, ...]")
    parser.add_argument("--output", required=True)
    parser.add_argument("--snap-window", type=float, default=0.5)
    parser.add_argument("--crossfade", type=float, default=0.02)
    args = parser.parse_args()

    with open(args.sections) as f:
        sections = json.load(f)
    if len(sections) < 2:
        fail("need at least two sections")

    tracks = {}
    sr = None
    for s in sections:
        path = s["vocal"]
        if path in tracks:
            continue
        y, file_sr = sf.read(path, always_2d=True, dtype="float32")
        y = y.T  # (channels, samples)
        if sr is None:
            sr = file_sr
        elif file_sr != sr:
            y = librosa.resample(y, orig_sr=file_sr, target_sr=sr, res_type="soxr_vhq")
        tracks[path] = y
    length = min(y.shape[1] for y in tracks.values())
    channels = max(y.shape[0] for y in tracks.values())
    for path, y in tracks.items():
        y = y[:, :length]
        tracks[path] = np.repeat(y, channels, axis=0) if y.shape[0] < channels else y

    orig, _ = librosa.load(args.original, sr=sr, mono=True)

    cuts = []
    for s in sections[1:]:
        t = quietest_point(orig, sr, float(s["start"]), args.snap_window)
        if cuts and t <= cuts[-1]:
            fail(f"sections too close together to cut cleanly near {s['start']:.2f}s")
        cuts.append(t)

    # Per-section gain envelopes that sum (in power) to 1 everywhere, with an
    # equal-power crossfade centred on each cut.
    fade = max(1, int(args.crossfade * sr))
    out = np.zeros((channels, length), dtype=np.float32)
    bounds = [0] + [int(t * sr) for t in cuts] + [length]
    for k, s in enumerate(sections):
        g = np.zeros(length, dtype=np.float32)
        a, b = bounds[k], bounds[k + 1]
        g[a:b] = 1.0
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        # A cut can land past the end of the shortest take (e.g. a last section
        # starting in the final few ms) - clamp so the fade never goes negative.
        if k > 0:
            lo = min(max(0, a - fade // 2), length)
            n = max(0, min(fade, length - lo))
            g[lo:lo + n] = ramp[:n]
        if k < len(sections) - 1:
            lo = min(max(0, b - fade // 2), length)
            n = max(0, min(fade, length - lo))
            g[lo:lo + n] = 1.0 - ramp[:n]
        out += tracks[s["vocal"]] * np.sqrt(g)

    sf.write(args.output, out.T, sr, subtype="FLOAT")
    print(json.dumps({"cuts": cuts, "sample_rate": sr}))


if __name__ == "__main__":
    main()
