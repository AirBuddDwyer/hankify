#!/usr/bin/env python3
"""Vocal rider: automatic fader riding for a vocal track (a native stand-in for Waves Vocal Rider).

Measures the vocal's phrase-level loudness (a ~300 ms RMS window), compares it with a target
(default: the median level of the voiced parts, so the vocal is evened out around its own average)
and moves a gain curve to close the gap, limited to +UP / -DOWN dB. Gain falls quickly and rises
slowly, so loud words get pulled down fast and quiet ones are lifted gently. In silences and breaths
the gain relaxes back to 0 dB so noise is not boosted.

Needs numpy + soundfile, so run it with the RVC venv python (combine_stage.py does this itself).

    vocal_rider.py IN.wav OUT.wav [--up 6] [--down 8] [--strength 1.0] [--target-db N]
"""
import argparse
import sys

import numpy as np
import soundfile as sf

HOP_S = 0.01


def _smooth(desired, attack_s, release_s, relax_s, voiced):
    """Asymmetric one-pole smoothing of the gain curve (dB), hop rate. Gain relaxes to 0 when unvoiced."""
    a_att = np.exp(-HOP_S / attack_s)
    a_rel = np.exp(-HOP_S / release_s)
    a_rlx = np.exp(-HOP_S / relax_s)
    g = np.zeros_like(desired)
    cur = 0.0
    for i, d in enumerate(desired):
        if voiced[i]:
            a = a_att if d < cur else a_rel
        else:
            a = a_rlx
        cur = a * cur + (1 - a) * (d if voiced[i] else 0.0)
        g[i] = cur
    return g


def ride(x, sr, up_db=6.0, down_db=8.0, strength=1.0, target_db=None, window_s=0.3,
         attack_s=0.08, release_s=0.35, relax_s=0.6, floor_db=35.0):
    """x: float array (n,) or (n, ch). Returns (y, stats dict)."""
    mono = x if x.ndim == 1 else x.mean(axis=1)
    hop = max(1, int(sr * HOP_S))
    win = max(hop, int(sr * window_s)) | 1
    kernel = np.hanning(win)
    kernel /= kernel.sum()
    env = np.sqrt(np.convolve(mono.astype(np.float64) ** 2, kernel, mode="same"))
    level = 20 * np.log10(env[::hop] + 1e-9)
    if level.max() < -80:
        return x, {"voiced": 0.0}
    voiced = level > np.percentile(level, 95) - floor_db
    tgt = float(np.median(level[voiced])) if target_db is None else float(target_db)
    desired = np.clip(strength * (tgt - level), -down_db, up_db)
    g = _smooth(desired, attack_s, release_s, relax_s, voiced)
    gain = 10 ** (np.interp(np.arange(len(mono)), np.arange(len(g)) * hop, g) / 20)
    y = x * (gain if x.ndim == 1 else gain[:, None])
    after = 20 * np.log10(np.sqrt(np.convolve((y if y.ndim == 1 else y.mean(axis=1)).astype(np.float64) ** 2,
                                                 kernel, mode="same"))[::hop] + 1e-9)
    stats = {"voiced": float(voiced.mean()), "target_db": tgt,
             "std_before": float(level[voiced].std()), "std_after": float(after[voiced].std()),
             "gain_min": float(g.min()), "gain_max": float(g.max())}
    return y, stats


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--up", type=float, default=6.0, help="max boost, dB")
    p.add_argument("--down", type=float, default=8.0, help="max cut, dB")
    p.add_argument("--strength", type=float, default=1.0, help="0..1 fraction of the correction applied")
    p.add_argument("--target-db", type=float, help="target level (dBFS RMS); default = median of the voiced parts")
    args = p.parse_args()
    x, sr = sf.read(args.input, always_2d=False, dtype="float32")
    y, st = ride(x, sr, args.up, args.down, args.strength, args.target_db)
    sf.write(args.output, y, sr, subtype="FLOAT")
    if st.get("voiced"):
        print("vocal_rider: level spread {std_before:.2f} -> {std_after:.2f} dB (std over voiced), "
              "target {target_db:.1f} dB, gain {gain_min:+.1f}..{gain_max:+.1f} dB".format(**st), file=sys.stderr)
    else:
        print("vocal_rider: no vocal found, unchanged", file=sys.stderr)


if __name__ == "__main__":
    main()
