#!/usr/bin/env python3
"""Tonal-balance match for the finished mix (2026-10-22, audio-engineering advice: "dynamics are perfect but the
sonics are wrong, follow the Fletcher-Munson / pink-noise curve"; made standard for every song).

Measures the long-term third-octave spectrum of the mix and of the ORIGINAL song (the sum of its own separated
stems: instrumental + original vocal + backing), and prints an ffmpeg firequalizer filter that nudges the mix's
tonal balance toward the original's professionally mastered curve. Only the shape is matched, never the level
(the bands are normalised over 250 Hz-4 kHz), and gently: cuts and boosts are capped, boosts are blocked in the
top end where RVC grit lives (the "crunch" the expert heard), and bands the reference barely has (e.g. above a
lossy source's cutoff) are left alone. Settings: vocal_presets.json "tonal_match".

    tonal_match.py MIX REF1 [REF2 ...] [--max-cut 4] [--max-boost 2] [--no-boost-above 2500]

Prints one line: the filter (or "anull" when nothing needs changing). Report goes to stderr.
Needs numpy + soundfile, so combine_stage.py runs it with the RVC venv python.
"""
import argparse
import sys

import numpy as np
import soundfile as sf

BANDS = [40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500,
         3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000]
NFFT = 8192


def spectrum(paths):
    """Mean power spectrum of the mono sum of the given files (read in blocks, resampled bins mapped by Hz)."""
    sr_seen = None
    for p in paths:
        info = sf.info(p)
        sr_seen = sr_seen or info.samplerate
        if info.samplerate != sr_seen:
            sys.exit(f"tonal_match: sample rates differ ({p})")
    win = np.hanning(NFFT).astype(np.float32)
    streams = [sf.blocks(p, blocksize=NFFT * 64, dtype="float32", always_2d=True) for p in paths]
    acc, n = np.zeros(NFFT // 2 + 1), 0
    while True:
        got = [b for b in (next(s, None) for s in streams) if b is not None]
        if not got:
            break
        chunk = np.zeros(max(len(b) for b in got), dtype=np.float32)
        for b in got:
            chunk[:len(b)] += b.mean(1)
        frames = len(chunk) // NFFT
        if frames:
            x = chunk[:frames * NFFT].reshape(frames, NFFT) * win
            acc += (np.abs(np.fft.rfft(x, axis=1)) ** 2).sum(0)
            n += frames
    total = acc / max(n, 1)
    return np.fft.rfftfreq(NFFT, 1 / sr_seen), total


def bands_db(f, p):
    out = []
    for b in BANDS:
        sel = (f >= b / 2 ** (1 / 6)) & (f < b * 2 ** (1 / 6))
        out.append(10 * np.log10(p[sel].sum() + 1e-20))
    return np.array(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mix")
    ap.add_argument("refs", nargs="+")
    ap.add_argument("--max-cut", type=float, default=4.0)
    ap.add_argument("--max-boost", type=float, default=2.0)
    ap.add_argument("--no-boost-above", type=float, default=2500)
    ap.add_argument("--floor-db", type=float, default=35.0,
                    help="leave bands alone where the reference is this far under its 1 kHz band")
    a = ap.parse_args()

    mix = bands_db(*spectrum([a.mix]))
    ref = bands_db(*spectrum(a.refs))
    mid = [i for i, b in enumerate(BANDS) if 250 <= b <= 4000]
    diff = ref - mix
    diff -= diff[mid].mean()                                  # shape only, never overall level
    diff = np.convolve(np.pad(diff, 1, mode="edge"), [0.25, 0.5, 0.25], "valid")   # no narrow wiggles
    k = BANDS.index(1000)
    gains = []
    for i, b in enumerate(BANDS):
        g = diff[i]
        if ref[i] < ref[k] - a.floor_db or mix[i] < mix[k] - a.floor_db:
            g = 0.0
        if b > a.no_boost_above:
            g = min(g, 0.0)
        gains.append(float(np.clip(g, -a.max_cut, a.max_boost)))
    print("tonal_match: " + " ".join(f"{b if b < 1000 else f'{b / 1000:g}k'}:{g:+.1f}" for b, g in zip(BANDS, gains)),
          file=sys.stderr)
    if max(abs(g) for g in gains) < 0.25:
        print("anull")
        return
    entries = ";".join(f"entry({b},{g:.2f})" for b, g in zip(BANDS, gains))
    print(f"firequalizer=gain_entry='{entries}':gain='gain_interpolate(f)':zero_phase=on")


if __name__ == "__main__":
    main()
