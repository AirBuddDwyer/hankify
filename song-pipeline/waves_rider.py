#!/usr/bin/env python3
"""Run Waves Vocal Rider (Mono) offline on a wav: run_vr.py IN OUT [--target dB] [--range dB] [--attack Slow|Fast]"""
import argparse, os, sys, numpy as np, soundfile as sf, pedalboard
ap = argparse.ArgumentParser(); ap.add_argument("inp"); ap.add_argument("out")
ap.add_argument("--target", type=float); ap.add_argument("--range", type=float, default=6.0)
ap.add_argument("--attack", default="Slow"); a = ap.parse_args()
x, sr = sf.read(a.inp, dtype="float32", always_2d=True); x = x.mean(axis=1, keepdims=True).T
if a.target is None:   # default: median 300 ms level of the voiced parts
    w = int(sr * .3); lv = np.array([20*np.log10(np.sqrt((x[0, i:i+w]**2).mean())+1e-9) for i in range(0, x.shape[1]-w, w)])
    a.target = float(np.median(lv[lv > lv.max()-30]))
pl = pedalboard.load_plugin(os.path.expanduser("~/.vst3/yabridge/WaveShell1-VST3 17.1_x64.vst3"), plugin_name="Vocal Rider Mono")
pl.target = a.target; pl.range_max = a.range; pl.range_min = -a.range; pl.attack = a.attack + " "
y = pl(x, sr, reset=True); sf.write(a.out, y.T, sr, subtype="FLOAT")
print(f"vocal rider: target {a.target:.1f} dB, range ±{a.range}, {a.attack}", file=sys.stderr, flush=True)
os._exit(0)
