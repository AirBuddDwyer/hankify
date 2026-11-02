#!/usr/bin/env python3
""""New way" mix (2026-10-23):
treat the converted vocal like a freshly recorded lead and mix it into the record, instead of preset EQ +
LUFS match. THE DEFAULT since 2026-10-23:
combine_stage.py hands off to it when vocal_presets.json has "mode": "new" (COMBINE_WAY=old forces the old way).

    combine_new.py --vocals LEAD.wav --instrumental INST.wav --reference-vocals ORIG_LEAD.wav
                   --original SONG.flac --output OUT.mp3 [--backing BACKING.wav] [--space 10]
                   [--target-lufs -11] [--artist A] [--title T] [--original-artist O]

Chain: clip gain -> corrective EQ -> de-esser -> Waves Vocal Rider (+-3 dB) -> two gentle compressors ->
dynamic EQ on 2.5-4.5 kHz harshness -> light saturation -> level set on the 300 Hz-3 kHz band against the
original lead (not LUFS: RVC's missing highs fool it) -> plate + tempo-synced delay sends -> instrumental
ducked 1.5 dB at 2-4 kHz while the vocal sings -> mix (original stereo backing untouched) -> gentle tonal
match to the original song -> light bus compression -> limiter at the target loudness, -1 dBTP.
Needs the RVC venv (numpy, scipy, librosa, soundfile).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import librosa
import numpy as np
import scipy.signal as ss
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import combine_stage as cs  # noqa: E402  (integrated_lufs, make_reverb, ARTIST_NAMES, WAVES_PY)


def ff(args, what):
    r = subprocess.run(["ffmpeg", "-y", "-v", "error"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        sys.exit(f"combine_new: {what} failed:\n{r.stdout[-2000:]}")


def mono(path, sr):
    y, _ = librosa.load(path, sr=sr, mono=True)
    return y.astype(np.float64)


def active(y, sr, floor_db=40):
    """Frame mask of where the vocal is actually singing (within floor_db of its loud frames)."""
    hop = int(0.02 * sr)
    rms = librosa.feature.rms(y=y, frame_length=2 * hop, hop_length=hop)[0]
    db = 20 * np.log10(rms + 1e-9)
    return db > np.percentile(db, 95) - floor_db, hop


def band(y, sr, lo, hi, taps=2047):
    """Zero-phase (linear-phase FIR, delay-compensated) band, so x - band(x) is an exact complement."""
    h = ss.firwin(taps, [lo, hi], pass_zero=False, fs=sr)
    return ss.fftconvolve(y, h, mode="same")


def envelope(x, sr, att_ms, rel_ms):
    a, r = np.exp(-1 / (sr * att_ms / 1000)), np.exp(-1 / (sr * rel_ms / 1000))
    out, e = np.empty_like(x), 0.0
    for i, v in enumerate(x):        # one-pole follower; called on 10 ms control rate arrays only
        e = a * e + (1 - a) * v if v > e else r * e + (1 - r) * v
        out[i] = e
    return out


def band_rms(y, sr, mask, hop):
    b = band(y, sr, 300, 3000)
    fr = librosa.feature.rms(y=b, frame_length=2 * hop, hop_length=hop)[0]
    n = min(len(fr), len(mask))
    return float(np.sqrt(np.mean(fr[:n][mask[:n]] ** 2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocals", required=True)
    ap.add_argument("--instrumental", required=True)
    ap.add_argument("--reference-vocals", required=True, help="original lead vocal stem (level reference)")
    ap.add_argument("--original", help="the original song (tonal-match reference; default: the sum of the stems)")
    ap.add_argument("--backing", help="original backing-vocal stem, mixed in untouched (stereo)")
    ap.add_argument("--backing-ref", help="original backing stem: level-match --backing to it (300 Hz-3 kHz band) "
                                          "when --backing is a converted replacement, e.g. Gary")
    ap.add_argument("--output", required=True)
    ap.add_argument("--space", type=float, default=10.0, help="plate send %% (delay at 60%% of it); 0 = dry")
    ap.add_argument("--target-lufs", type=float, default=-11.0)
    ap.add_argument("--duck-db", type=float, default=1.5)
    ap.add_argument("--vocal-gain-db", type=float, default=0.0, help="added on top of the band level match")
    ap.add_argument("--stereo-extra", help="stereo track summed into the mix as-is")
    ap.add_argument("--artist"); ap.add_argument("--title"); ap.add_argument("--original-artist")
    a = ap.parse_args()
    sr = sf.info(a.instrumental).samplerate
    tmp = tempfile.mkdtemp(prefix="combine_new_", dir=os.path.dirname(os.path.abspath(a.output)))
    rep = {}
    try:
        # 1. clip gain: active singing to -20 dBFS RMS, so every later threshold means the same thing
        v = mono(a.vocals, sr)
        m, hop = active(v, sr)
        fr = librosa.feature.rms(y=v, frame_length=2 * hop, hop_length=hop)[0]
        g = 10 ** (-20 / 20) / max(np.sqrt(np.mean(fr[:len(m)][m[:len(fr)]] ** 2)), 1e-9)
        p = os.path.join(tmp, "v0.wav"); sf.write(p, v * g, sr, subtype="FLOAT")
        # 2. corrective EQ + de-esser (before the rider, per the guide)
        q = os.path.join(tmp, "v1.wav")
        ff(["-i", p, "-af", "highpass=f=80:poles=2,equalizer=f=280:t=q:w=1.0:g=-2,"
            "deesser=i=0.4:m=0.4:f=0.5", "-c:a", "pcm_f32le", q], "eq/de-esser")
        # 3. Waves Vocal Rider, gentle range (native rider if the Waves host fails)
        rdn = os.path.join(tmp, "v2.wav")
        ok = False
        if os.path.isfile(cs.WAVES_PY):
            try:   # the Waves host can hang on near-silent input: fall back to the native rider
                r = subprocess.run([cs.WAVES_PY, os.path.join(HERE, "waves_rider.py"), q, rdn, "--range", "3",
                                    "--attack", "Slow"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   timeout=600)
                ok = r.returncode == 0 and os.path.isfile(rdn) and os.path.getsize(rdn) > 1000
            except subprocess.TimeoutExpired:
                ok = False
        if not ok:
            import hankify_config
            subprocess.run([hankify_config.RVC_PYTHON, os.path.join(HERE, "vocal_rider.py"), q, rdn,
                            "--up", "3", "--down", "3"], check=True, stdout=subprocess.DEVNULL)
        rep["rider"] = "waves +-3" if ok else "native +-3"
        # 4. two gentle compressors (~3 dB then ~1-2 dB on loud lines)
        c = os.path.join(tmp, "v3.wav")
        ff(["-i", rdn, "-af", "acompressor=threshold=-24dB:ratio=3:attack=20:release=100:knee=4dB,"
            "acompressor=threshold=-18dB:ratio=2:attack=8:release=60:knee=2dB", "-c:a", "pcm_f32le", c], "compressors")
        v = mono(c, sr)
        # 5. dynamic EQ: cut 2.5-4.5 kHz up to 3 dB only when it pokes above its usual share of the voice
        hb = band(v, sr, 2500, 4500)
        ctl = int(0.01 * sr)
        n = len(v) // ctl
        eb = np.sqrt(np.mean(hb[:n * ctl].reshape(n, ctl) ** 2, 1)) + 1e-9
        ev = np.sqrt(np.mean(v[:n * ctl].reshape(n, ctl) ** 2, 1)) + 1e-9
        ratio = 20 * np.log10(eb / ev)
        live = ev > np.percentile(ev, 95) * 0.03
        base = np.median(ratio[live]) if live.any() else 0.0
        cut = np.clip((ratio - base - 2.0) * 0.7, 0, 3) * live
        cut = envelope(cut, 100, 10, 150)
        gcurve = np.repeat(10 ** (-cut / 20), ctl)
        gcurve = np.pad(gcurve, (0, len(v) - len(gcurve)), mode="edge")
        v = v + hb * (gcurve - 1)
        rep["dyn_eq_max_cut_db"] = round(float(cut.max()), 2)
        rep["dyn_eq_time_cutting_pct"] = round(float((cut > 0.5).mean() * 100), 1)
        # 6. light saturation (25% parallel tanh)
        pk = np.max(np.abs(v)) + 1e-9
        v = 0.75 * v + 0.25 * pk * np.tanh(1.2 * v / pk) / np.tanh(1.2)
        # 7. level: match the original lead in the 300 Hz-3 kHz band (where both voices actually are)
        ref = mono(a.reference_vocals, sr)
        mr, hop = active(ref, sr)
        mv, _ = active(v, sr)
        gain_db = 20 * np.log10(band_rms(ref, sr, mr, hop) / max(band_rms(v, sr, mv, hop), 1e-9))
        gain_db += a.vocal_gain_db
        v *= 10 ** (gain_db / 20)
        rep["vocal_level_db"] = round(float(gain_db), 2)
        vox = os.path.join(tmp, "vox.wav"); sf.write(vox, v, sr, subtype="FLOAT")
        # 8. space: plate + tempo-synced delay sends (stereo), both filtered
        n_total = len(v)
        wet = np.zeros((n_total, 2))
        if a.space > 0:
            plate = cs.make_reverb(vox, tmp, {"decay_s": 1.2, "predelay_ms": 20, "highpass_hz": 300, "lowpass_hz": 6000})
            pw, _ = librosa.load(plate, sr=sr, mono=False)
            pw = pw.T[:n_total]
            pw *= np.sqrt(np.mean(v ** 2)) / (np.sqrt(np.mean(pw ** 2)) + 1e-9) * a.space / 100
            wet[:len(pw)] += pw
            tempo = float(np.atleast_1d(librosa.beat.beat_track(y=mono(a.instrumental, 22050), sr=22050)[0])[0])
            q4 = 60.0 / tempo
            dsrc = band(v, sr, 300, 5000)
            for ch, t in ((0, q4), (1, q4 * 1.5)):          # 1/4 left, dotted 1/4 right
                d = int(t * sr)
                tap = np.zeros(n_total)
                for k, fb in enumerate((1.0, 0.35)):
                    off = d * (k + 1)
                    if off < n_total:
                        tap[off:] += dsrc[:n_total - off] * fb
                wet[:, ch] += tap * a.space * 0.6 / 100
            rep["tempo_bpm"] = round(tempo, 1)
        rep["space_pct"] = a.space
        # 9. instrumental: duck 2-4 kHz while the vocal sings
        inst, _ = librosa.load(a.instrumental, sr=sr, mono=False)
        inst = np.atleast_2d(inst).astype(np.float64)
        if inst.shape[0] == 1:
            inst = np.vstack([inst, inst])
        n = min(inst.shape[1], n_total)
        ev = np.sqrt(np.mean(v[:n // ctl * ctl].reshape(-1, ctl) ** 2, 1)) + 1e-9
        evdb = 20 * np.log10(ev)
        act = np.clip((evdb - (np.percentile(evdb, 95) - 25)) / 12, 0, 1)
        act = envelope(act, 100, 15, 250)
        duck = np.repeat(10 ** (-a.duck_db * act / 20), ctl)
        duck = np.pad(duck, (0, n - len(duck)), mode="edge")[:n]
        for ch in range(2):
            b = band(inst[ch, :n], sr, 2000, 4000)
            inst[ch, :n] = inst[ch, :n] + b * (duck - 1)
        # 10. mix
        mix = inst[:, :n].T.copy()
        mix += np.column_stack((v[:n], v[:n]))
        mix += wet[:n]
        if a.backing:
            bk, _ = librosa.load(a.backing, sr=sr, mono=False)
            bk = np.atleast_2d(bk)
            if bk.shape[0] == 1:
                bk = np.vstack([bk, bk])
            if a.backing_ref:
                br = mono(a.backing_ref, sr)
                mb, hb_ = active(br, sr)
                bm = bk.mean(0)
                mbm, _ = active(bm, sr)
                bg = 20 * np.log10(band_rms(br, sr, mb, hb_) / max(band_rms(bm, sr, mbm, hb_), 1e-9))
                bk = bk * 10 ** (bg / 20)
                rep["backing_level_db"] = round(float(bg), 2)
            k = min(n, bk.shape[1])
            mix[:k] += bk[:, :k].T
        if a.stereo_extra:
            ex, _ = librosa.load(a.stereo_extra, sr=sr, mono=False)
            ex = np.atleast_2d(ex)
            ex = np.vstack([ex, ex]) if ex.shape[0] == 1 else ex
            k = min(n, ex.shape[1])
            mix[:k] += ex[:, :k].T
        premix = os.path.join(tmp, "mix.wav"); sf.write(premix, mix, sr, subtype="FLOAT")
        # 11. mix bus: gentle tonal match to the original song, light bus compression
        refs = [a.original] if a.original else [a.instrumental, a.reference_vocals] + ([a.backing_ref or a.backing] if a.backing else [])
        refs = [r for r in refs if sf.info(r).samplerate == sr] or [a.instrumental]   # tonal_match needs one rate
        tm = subprocess.run([sys.executable, os.path.join(HERE, "tonal_match.py"), premix, *refs,
                             "--max-cut", "3", "--max-boost", "3", "--no-boost-above", "20000"],
                            capture_output=True, text=True)
        tone = tm.stdout.strip().splitlines()[-1] if tm.returncode == 0 and tm.stdout.strip() else "anull"
        rep["tonal_match"] = (tm.stderr.strip().splitlines() or ["(failed)"])[-1] if tm.returncode == 0 else f"failed: {tm.stderr[-300:]}"
        bus = os.path.join(tmp, "bus.wav")
        pre = cs.integrated_lufs(premix) or -14.0
        ff(["-i", premix, "-af", f"volume={-18 - pre:.2f}dB,{tone},"
            "acompressor=threshold=-24dB:ratio=1.5:attack=30:release=250:knee=6dB", "-c:a", "pcm_f32le", bus], "bus")
        # 12. limiter at the target loudness, true peak about -1 dBTP (two passes to land on target)
        lim = os.path.join(tmp, "lim.wav")
        up = a.target_lufs - (cs.integrated_lufs(bus) or -18.0)
        for _ in range(2):
            ff(["-i", bus, "-af", f"volume={up:.2f}dB,alimiter=limit=0.89:attack=5:release=80:level=disabled",
                "-c:a", "pcm_f32le", lim], "limiter")
            got = cs.integrated_lufs(lim) or a.target_lufs
            up += a.target_lufs - got
        rep["master_lufs"] = round(float(got), 2)
        meta = []
        if a.artist:
            meta += ["-metadata", "artist=" + " & ".join(cs.ARTIST_NAMES.get(x, x) for x in a.artist.split(" & "))]
        if a.title:
            meta += ["-metadata", f"title={a.title}"]
        if a.original_artist:
            meta += ["-metadata", f"TOPE={a.original_artist}"]
        ff(["-i", lim, "-c:a", "libmp3lame", "-b:a", "320k", *meta, a.output], "mp3 export")
        print("combine_new: " + json.dumps(rep))
        print(f"combine_new: wrote {a.output}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
