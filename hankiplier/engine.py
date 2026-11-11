#!/usr/bin/env python3
"""Hankiplier engine - runs in the RVC venv (whisper, librosa, sklearn, torch).

    engine.py analyze <project_dir>   vocal stem + parsed lyrics -> plan.json
    engine.py build   <project_dir>   plan.json + casting + renders -> final MP3

This is the multi-voice method that was worked out by hand song by song:
  1. Whisper `small` word timestamps on the UVR vocal stem.
  2. In-order (Needleman-Wunsch) alignment of Genius words to Whisper words; unmatched words get
     times interpolated from their matched neighbours.
  3. Consecutive lines with the same performer(s) become one section.
  4. A 16-component MFCC(+delta) GMM per performer, trained on that
     performer's confidently aligned lines, picks each cut: the point where
     the outgoing singer's evidence stops and the incoming one's starts,
     nudged toward quiet frames.
  5. Genius-marked ad-libs in parentheses (e.g. Big Sean's "(«Swerve»)")
     become short sections wherever the classifier clearly hears that
     performer in the gaps.
  6. build: per-performer takes (voice render, gain-matched; blends for
     "everyone" lines; or the untouched original stem), then the song
     pipeline's own splice_stage (fixed cuts) + combine_stage.
"""
import json
import os
import re
import subprocess
import sys
import difflib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hk_paths  # noqa: E402

PIPELINE_DIR = hk_paths.PIPELINE_DIR
HOP = 0.01  # analysis frame = 10 ms
ORIGINAL = "__original__"


def log(msg):
    print(msg, flush=True)


def load(pdir, name, default=None):
    p = os.path.join(pdir, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)


def save(pdir, name, obj):
    with open(os.path.join(pdir, name), "w") as f:
        json.dump(obj, f, indent=1)


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def pkey(perfs):
    return " + ".join(perfs)


# --------------------------------------------------------------------------- analyze

def transcribe(pdir, vocals):
    cache = os.path.join(pdir, "whisper_words.json")
    if os.path.exists(cache):
        log("Using cached Whisper transcript")
        return json.load(open(cache))
    import torch
    import whisper
    device = "cpu"
    # The app sets HANKIPLIER_CPU while voice renders share the 4 GB GPU -
    # Whisper on the GPU alongside RVC runs it out of memory.
    if torch.cuda.is_available() and not os.environ.get("HANKIPLIER_CPU"):
        free, _ = torch.cuda.mem_get_info()
        if free > 2.6e9:
            device = "cuda"
    # HANKIPLIER_WHISPER_MODEL: model size (default "small"; the CI uses "tiny" to stay fast on CPU).
    # HANKIPLIER_WHISPER_DIR: where the model file lives (the Windows package keeps it inside its folder);
    # unset = Whisper's own default cache (~/.cache/whisper).
    size = os.environ.get("HANKIPLIER_WHISPER_MODEL", "small")
    log(f"Transcribing vocals with Whisper {size} on {device} (a few minutes on CPU)...")
    m = whisper.load_model(size, device=device, download_root=os.environ.get("HANKIPLIER_WHISPER_DIR") or None)
    # HANKIPLIER_LANG: songs not in English (e.g. "es" for Despacito) - with "en" Whisper
    # translates instead of transcribing, and the Genius alignment falls apart.
    r = m.transcribe(vocals, word_timestamps=True, language=os.environ.get("HANKIPLIER_LANG", "en"))
    ww = [(w["word"], w["start"], w["end"]) for seg in r["segments"] for w in seg["words"]]
    json.dump(ww, open(cache, "w"))
    return ww


def _difflib_pairs(a, b):
    """Old matcher, kept for comparison: greedy longest-block first. With repeated choruses the
    final chorus can lock onto the first sung one and crush everything in between (The Way You
    Move: verse 2 squeezed into 0.7 s)."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(i + k, j + k) for i, j, n in sm.get_matching_blocks() for k in range(n)]


def _dp_pairs(a, b):
    """In-order global alignment (Needleman-Wunsch) of Genius words a vs Whisper words b that
    maximises the total match score, so a repeated chorus can't jump ahead of a whole verse:
    the verse's own matched words outweigh it. Leading/trailing Whisper words are free."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    MATCH, NEAR, MISS, GAP = 2.0, 1.0, -1.0, -0.6
    def score(x, y):
        if x == y:
            return MATCH
        if len(x) >= 4 and len(y) >= 4 and x[:3] == y[:3]:
            return NEAR                                    # "feelin"/"feeling", "thang"/"thing"
        return MISS
    H = np.zeros((n + 1, m + 1))
    P = np.zeros((n + 1, m + 1), dtype=np.int8)            # 1 diag, 2 up (skip lyric word), 3 left (skip heard word)
    H[1:, 0] = GAP * np.arange(1, n + 1); P[1:, 0] = 2
    P[0, 1:] = 3                                           # free leading Whisper words
    for i in range(1, n + 1):
        ai = a[i - 1]; Hi, Hp, Pi = H[i], H[i - 1], P[i]
        for j in range(1, m + 1):
            d = Hp[j - 1] + score(ai, b[j - 1])
            u = Hp[j] + GAP
            l = Hi[j - 1] + (0.0 if i == n else GAP)       # free trailing Whisper words
            if d >= u and d >= l:
                Hi[j] = d; Pi[j] = 1
            elif u >= l:
                Hi[j] = u; Pi[j] = 2
            else:
                Hi[j] = l; Pi[j] = 3
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        p = P[i, j]
        if p == 1:
            if score(a[i - 1], b[j - 1]) > 0:
                pairs.append((i - 1, j - 1))
            i -= 1; j -= 1
        elif p == 2:
            i -= 1
        else:
            j -= 1
    pairs = pairs[::-1]
    # Drop isolated one-word matches (a lone "the"/"I" matched far from its line drags the
    # interpolation with it): keep a match if another one sits within 3 lyric / 4 heard words
    # ("Ayy, wassup, girl?" vs heard "What's up girl?" keeps "girl"), or the word is long.
    def near(k, i, j):
        return any(p != (i, j) and abs(p[0] - i) <= 3 and abs(p[1] - j) <= 4 for p in pairs[max(0, k - 4):k + 5])
    kept = [(i, j) for k, (i, j) in enumerate(pairs) if len(a[i]) >= 6 or near(k, i, j)]
    return kept or pairs


MATCHER = os.environ.get("HANKIPLIER_MATCHER", "dp")


def squeeze_report(lines, times, min_rate=0.06):
    """Runs of >= 3 lines laid out faster than min_rate s/word: the signature of a misalignment
    (a whole verse crushed into a second). Returns [(first line idx, last line idx, seconds)]."""
    bad, run = [], []
    for i, (ln, t) in enumerate(zip(lines, times)):
        nxt = times[i + 1]["start"] if i + 1 < len(times) else None
        nw = max(1, len(ln["words"]))
        fast = nxt is not None and (nxt - t["start"]) / nw < min_rate
        if fast:
            run.append(i)
        elif run:
            if len(run) >= 3:
                bad.append((run[0], run[-1], round(times[run[-1]]["start"] - times[run[0]]["start"], 2)))
            run = []
    if len(run) >= 3:
        bad.append((run[0], run[-1], round(times[run[-1]]["start"] - times[run[0]]["start"], 2)))
    return bad


def align(lines, ww, duration, matcher=None):
    """Per-line (start, end, reliable) from Genius words vs Whisper words."""
    gw = []  # (norm word, line idx)
    for i, ln in enumerate(lines):
        for w in ln["words"]:
            for part in re.split(r"[-–—]", w):  # "O-o-o-okay" -> ..., "okay"
                n = norm(part)
                if n:
                    gw.append((n, i))
    wn = [norm(w[0]) for w in ww]
    pairs = (_difflib_pairs if (matcher or MATCHER) == "difflib" else _dp_pairs)([g[0] for g in gw], wn)
    t_start = [None] * len(gw)
    t_end = [None] * len(gw)
    for a, b in pairs:
        t_start[a] = ww[b][1]
        t_end[a] = ww[b][2]
    matched = sum(1 for t in t_start if t is not None)
    log(f"Aligned {matched}/{len(gw)} lyric words to the recording")

    # interpolate unmatched words between matched neighbours
    known = [i for i, t in enumerate(t_start) if t is not None]
    if not known:
        raise SystemExit("Whisper didn't match any lyrics - is this the right song/lyrics?")
    ks = np.array(known)
    st = np.interp(np.arange(len(gw)), ks, [t_start[i] for i in known])
    en = np.interp(np.arange(len(gw)), ks, [t_end[i] for i in known])
    # before the first / after the last matched word np.interp would pin every word to that one
    # time (Workout Plan: three girls' lines collapsed onto 3.48 s) - lay them out at ~0.3 s/word
    RATE, k0, k1 = 0.3, known[0], known[-1]
    for k in range(k0):
        st[k] = max(0.0, t_start[k0] - (k0 - k) * RATE)
        en[k] = st[k] + RATE
    for k in range(k1 + 1, len(gw)):
        st[k] = t_end[k1] + (k - k1 - 1) * RATE
        en[k] = st[k] + RATE
        if duration:
            st[k], en[k] = min(st[k], duration), min(en[k], duration)
    out = []
    for li in range(len(lines)):
        idx = [k for k, g in enumerate(gw) if g[1] == li]
        if not idx:
            out.append(None)
            continue
        m = sum(1 for k in idx if t_start[k] is not None)
        out.append({"start": float(st[idx[0]]), "end": float(en[idx[-1]]),
                    "reliable": m >= max(2, 0.4 * len(idx))})
    # lines with no words at all: borrow neighbour's time
    for i, o in enumerate(out):
        if o is None:
            prev = next((out[j] for j in range(i - 1, -1, -1) if out[j]), None)
            t = prev["end"] if prev else 0.0
            out[i] = {"start": t, "end": t, "reliable": False}
    return out


def features(vocals):
    import librosa
    y, sr = librosa.load(vocals, sr=16000, mono=True)
    hop = int(sr * HOP)
    M = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20, hop_length=hop, n_fft=512)
    F = np.vstack([M, librosa.feature.delta(M)]).T
    rms = librosa.feature.rms(y=y, frame_length=512, hop_length=hop)[0]
    db = 20 * np.log10(rms + 1e-9)
    n = min(len(F), len(db))
    return F[:n], db[:n], len(y) / sr


def train_models(F, db, lines, times):
    from sklearn.mixture import GaussianMixture
    act = db > -35
    # Prefer lines with no credited ad-libs: otherwise e.g. the sample's
    # model learns Big Sean's "Swerve"s and can never tell them apart.
    clean, dirty = {}, {}
    for ln, t in zip(lines, times):
        if len(ln["performers"]) != 1 or not t["reliable"]:
            continue
        a, b = int(t["start"] / HOP), int(t["end"] / HOP)
        if b - a < 20:
            continue
        (dirty if ln.get("adlibs") else clean).setdefault(ln["performers"][0], []).append((a, b))
    models = {}
    for p in list(dict.fromkeys(list(clean) + list(dirty))):
        spans = clean.get(p, [])
        if sum(act[a:b].sum() for a, b in spans) < 300:
            spans = spans + dirty.get(p, [])
        X = np.vstack([F[a:b][act[a:b]] for a, b in spans])
        if len(X) < 300:  # < 3 s of voiced frames - not enough to learn a voice
            log(f"  {p}: only {len(X) / 100:.1f}s of clean lines - no voice model")
            continue
        models[p] = GaussianMixture(16, covariance_type="diag", random_state=0,
                                    reg_covar=1e-3).fit(X)
        log(f"  {p}: voice model from {len(X) / 100:.0f}s")
    L = {p: m.score_samples(F) for p, m in models.items()}
    return L


def pick_cut(out_p, in_p, lo, hi, L, db):
    """Best cut in [lo, hi] seconds. With voice models: maximise (outgoing
    evidence before, incoming after), nudged to quiet frames. Else: quietest."""
    a, b = max(1, int(lo / HOP)), min(len(db) - 1, int(hi / HOP))
    if b <= a:
        return lo
    if out_p in L and in_p in L:
        d = np.clip(L[out_p] - L[in_p], -30, 30)
        best, bk = None, a
        for k in range(a, b):
            s = d[max(0, k - 80):k].mean() - d[k:k + 80].mean() - 0.3 * (db[k - 2:k + 3].mean() + 20)
            if best is None or s > best:
                best, bk = s, k
        return bk * HOP
    k = a + int(np.argmin([db[max(0, j - 2):j + 3].mean() for j in range(a, b)]))
    return k * HOP


def find_adlibs(seg_p, adlib_ps, s, e, L, db):
    """Runs (>= 0.3 s) inside [s, e] where the section's performer is clearly
    NOT the one singing, in a section whose lyrics credit ad-libs to someone.
    Shouted ad-libs rarely sound like the same artist's verse, so the test is
    "not the section singer" (any other voice model wins), and the run goes to
    whichever credited ad-lib performer scores higher there.
    Returns [(start, end, performer)]."""
    cand = [p for p in adlib_ps if p in L]
    others = [p for p in L if p != seg_p]
    if seg_p not in L or not cand or not others:
        return []
    act = db > -35
    step = 5  # 50 ms windows
    a, b = int(s / HOP), int(e / HOP)
    lab = []
    for k in range(a, b, step):
        m = act[k:k + step]
        if m.mean() < 0.3:
            lab.append(None)
            continue
        own = L[seg_p][k:k + step][m].mean()
        best = max(L[o][k:k + step][m].mean() for o in others)
        lab.append(best > own + 2)
    runs, st = [], None
    for i, v in enumerate(lab + [False]):
        if v and st is None:
            st = i
        if not v and st is not None:
            runs.append([st, i])
            st = None
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 5:  # bridge gaps <= 0.25 s
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    found = []
    for r0, r1 in merged:
        if (r1 - r0) * step < 30:
            continue
        k0, k1 = a + r0 * step, a + r1 * step
        who = max(cand, key=lambda p: L[p][k0:k1].mean())
        found.append((k0 * HOP, k1 * HOP, who))
    return found


def snap_quiet(t, db, radius=0.12):
    a, b = max(0, int((t - radius) / HOP)), min(len(db), int((t + radius) / HOP))
    return (a + int(np.argmin(db[a:b]))) * HOP if b > a else t


def analyze(pdir):
    proj = load(pdir, "project.json")
    vocals = proj["stems"]["vocals"]
    lines = proj["parts"]["lines"]
    ww = transcribe(pdir, vocals)
    log("Computing voice features...")
    F, db, duration = features(vocals)
    times = align(lines, ww, duration)
    # alarm: a squeezed run that crosses a singer change means that handoff is guesswork
    warnings = []
    for a, b, secs in squeeze_report(lines, times):
        rng = range(a, min(len(lines), b + 2))
        keys = [pkey(lines[i]["performers"]) for i in rng]
        if any(x != y for x, y in zip(keys, keys[1:])):
            warnings.append(f"~{times[a]['start']:.0f}s: lines {a}-{b} ({lines[a]['text'][:40]!r}...) "
                            f"squeezed into {secs}s across a singer change - check this handoff by ear")
    for w in warnings:
        log("WARNING " + w)
    if warnings:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "warnings.log"), "a") as f:
            for w in warnings:
                f.write(f"{os.path.basename(os.path.normpath(pdir))}: {w}\n")
    log("Training a voice model per performer:")
    L = train_models(F, db, lines, times)

    # group consecutive lines by performer set
    segs = []
    for ln, t in zip(lines, times):
        k = pkey(ln["performers"])
        if segs and segs[-1]["key"] == k:
            segs[-1]["lines"].append((ln, t))
        else:
            segs.append({"key": k, "perfs": ln["performers"], "lines": [(ln, t)]})

    starts = [0.0]
    for prev, nxt in zip(segs, segs[1:]):
        te = prev["lines"][-1][1]["end"]
        ts = nxt["lines"][0][1]["start"]
        lo, hi = min(te, ts) - 0.6, max(te, ts) + 0.6
        # unreliable edges: widen the search
        if not (prev["lines"][-1][1]["reliable"] and nxt["lines"][0][1]["reliable"]):
            lo, hi = lo - 1.0, hi + 1.0
        lo = max(lo, starts[-1] + 0.3)
        op = prev["perfs"][0] if len(prev["perfs"]) == 1 else None
        ip = nxt["perfs"][0] if len(nxt["perfs"]) == 1 else None
        starts.append(round(pick_cut(op, ip, lo, max(hi, lo + 0.1), L, db), 2))

    plan = []
    for i, sg in enumerate(segs):
        s = starts[i]
        e = starts[i + 1] if i + 1 < len(segs) else duration
        text = " / ".join(ln["text"] for ln, _ in sg["lines"])
        entry = {"start": s, "performers": sg["perfs"], "lyric": text,
                 "confident": all(t["reliable"] for _, t in sg["lines"][:1] + sg["lines"][-1:])}
        plan.append(entry)
        if len(sg["perfs"]) == 1:
            found = []
            for ln, t in sg["lines"]:
                if not ln.get("adlibs"):
                    continue
                # search only around lines that carry the ad-lib, clear of the
                # section edges (the main cut handles those)
                lo = max(s + 0.5, t["start"] - 0.3)
                hi = min(e - 0.5, t["end"] + 1.2)
                if hi - lo > 0.3:
                    found += find_adlibs(sg["perfs"][0], ln["adlibs"], lo, hi, L, db)
            found.sort()
            dedup = []
            for f in found:
                if dedup and f[0] <= dedup[-1][1]:
                    if f[1] > dedup[-1][1]:
                        dedup[-1] = (dedup[-1][0], f[1], dedup[-1][2])
                    continue
                dedup.append(f)
            for a0, a1, ap in dedup:
                a0, a1 = snap_quiet(a0, db), snap_quiet(a1, db)
                if a1 - a0 < 0.25:
                    continue
                plan.append({"start": round(a0, 2), "performers": [ap], "lyric": "(ad-lib)",
                             "confident": False, "adlib": True})
                plan.append({"start": round(a1, 2), "performers": sg["perfs"], "lyric": "…" + text[-40:],
                             "confident": True, "resume": True})
    plan.sort(key=lambda p: p["start"])
    # merge neighbours that ended up with the same performers
    merged = []
    for p in plan:
        if merged and merged[-1]["performers"] == p["performers"]:
            continue
        if merged and p["start"] - merged[-1]["start"] < 0.05:
            merged[-1] = p
            continue
        merged.append(p)
    save(pdir, "plan.json", {"duration": duration, "sections": merged,
                             "parts_hash": proj.get("parts_hash"), "warnings": warnings})
    n_ad = sum(1 for p in merged if p.get("adlib"))
    log(f"Plan: {len(merged)} sections ({n_ad} ad-libs). Review it, then build.")


# --------------------------------------------------------------------------- build

def build(pdir):
    import librosa
    import soundfile as sf
    proj = load(pdir, "project.json")
    plan = load(pdir, "plan.json")
    secs = plan["sections"]
    duration = plan["duration"]
    casting = proj["casting"]          # performer -> {"voice", "pitch"}
    renders = proj["renders"]          # voice key -> converted vocal path
    vocals = proj["stems"]["vocals"]
    target_sr = 40000

    def vkey(perf):
        c = casting[perf]
        if c["voice"] == ORIGINAL:
            return ORIGINAL
        return f'{c["voice"]}|{c.get("pitch", 0)}' + ("|bv" if c.get("backing") else "")

    # "Keep backing" singers: their render converted only the LEAD vocal; the
    # original backing vocals get added back under it in their sections.
    split = proj.get("backing_stems") or {}
    stem_cache = {}
    try:   # vocal_presets.json "mode": "new" = combine_new.py mixes the lead and the original backing separately
        new_way = json.load(open(os.path.join(PIPELINE_DIR, "vocal_presets.json"))).get("mode") == "new" \
            and os.environ.get("COMBINE_WAY") != "old"
    except (OSError, ValueError):
        new_way = False
    lead_only = new_way or bool(os.environ.get("HANKIPLIER_LEAD_ONLY"))

    # "Backing voice" (any RVC voice): the backing stem converted to a character replaces the original backing
    bvoice = proj.get("backing_voice")
    br = proj.get("backing_render") or {}
    conv_backing = br.get("path") if bvoice and br.get("voice") == bvoice \
        and os.path.isfile(br.get("path") or "") else None

    def backing_track(sr_out=None):
        """(samples, 2) backing at sr_out (None = the stem's own rate): the original stem, or its
        conversion level-matched to the original over the keep-backing sections."""
        bs, bsr = sf.read(split["backing"], always_2d=True)
        if sr_out and bsr != sr_out:
            bs = librosa.resample(bs.T, orig_sr=bsr, target_sr=sr_out, res_type="soxr_vhq").T
        sr = sr_out or bsr
        if bs.shape[1] == 1:
            bs = np.column_stack((bs[:, 0], bs[:, 0]))
        if not conv_backing:
            return bs, sr
        cy, csr = sf.read(conv_backing, always_2d=True)
        cy = cy.mean(1)
        if csr != sr:
            cy = librosa.resample(cy, orig_sr=csr, target_sr=sr, res_type="soxr_vhq")
        cy = np.pad(cy, (0, max(0, len(bs) - len(cy))))[:len(bs)]
        sel = np.zeros(len(bs), bool)
        for s0, e0, perfs in spans:
            if any(vkey(p).endswith("|bv") for p in perfs):
                sel[int(s0 * sr):int(e0 * sr)] = True
        if not sel.any():
            sel[:] = True
        o, c = bs[sel].mean(1), cy[sel]
        g = float(np.sqrt(np.mean(o ** 2)) / max(np.sqrt(np.mean(c ** 2)), 1e-9))
        log(f"  backing voice {bvoice.rsplit('.', 1)[0]}: {20 * np.log10(max(g, 1e-9)):+.1f} dB to match the original backing")
        return np.column_stack((cy * g, cy * g)), sr

    def stem_audio(which):
        if which not in stem_cache:
            if which == "backing":
                stem_cache[which] = backing_track(target_sr)[0].mean(1)
                return stem_cache[which]
            y, sr = sf.read(split[which], always_2d=True)
            y = y.mean(1)
            if sr != target_sr:
                y = librosa.resample(y, orig_sr=sr, target_sr=target_sr, res_type="soxr_vhq")
            stem_cache[which] = y
        return stem_cache[which]

    orig, osr = librosa.load(vocals, sr=None, mono=True)
    cache = {}

    def voice_audio(k):
        if k not in cache:
            path = vocals if k == ORIGINAL else renders[k]
            y, sr = sf.read(path, always_2d=True)
            y = y.mean(1)
            if sr != target_sr:
                y = librosa.resample(y, orig_sr=sr, target_sr=target_sr, res_type="soxr_vhq")
            cache[k] = y
        return cache[k]

    spans = []  # (start, end, performers)
    for i, s in enumerate(secs):
        e = secs[i + 1]["start"] if i + 1 < len(secs) else duration
        spans.append((s["start"], e, s["performers"]))

    # level-match every voice so its converted/original loudness ratio equals
    # the lead's (the performer with the most singing time)
    rms = lambda x: float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    per_voice = {}
    for s, e, perfs in spans:
        if len(perfs) == 1:
            per_voice.setdefault(vkey(perfs[0]), []).append((s, e))
    ratio = {}
    for k, sp in per_voice.items():
        if k == ORIGINAL:
            ratio[k] = 1.0
            continue
        y = voice_audio(k)
        c = np.concatenate([y[int(a * target_sr):int(b * target_sr)] for a, b in sp])
        if k.endswith("|bv"):  # converted lead vs the original lead stem
            ref = stem_audio("lead")
            o = np.concatenate([ref[int(a * target_sr):int(b * target_sr)] for a, b in sp])
        else:
            o = np.concatenate([orig[int(a * osr):int(b * osr)] for a, b in sp])
        ratio[k] = rms(c) / max(rms(o), 1e-9)
    lead = max(per_voice, key=lambda k: sum(b - a for a, b in per_voice[k])) if per_voice else None
    target = 1.0 if ORIGINAL in ratio else ratio.get(lead, 1.0)
    gain = {k: (target / r if r > 0 else 1.0) for k, r in ratio.items()}
    for k, g in gain.items():
        name = "original vocal" if k == ORIGINAL else k.split("|")[0].rsplit(".", 1)[0]
        if k.endswith("|bv"):
            name += f" (lead, + {bvoice.rsplit('.', 1)[0] if conv_backing else 'original'} backing)"
        log(f"  level {name}: {20 * np.log10(g):+.1f} dB")

    takes_dir = os.path.join(pdir, "takes")
    os.makedirs(takes_dir, exist_ok=True)
    take_paths = {}

    def take_for(perfs):
        key = pkey(perfs)
        if key in take_paths:
            return take_paths[key]
        # several performers cast to the same voice (e.g. three girls all Lois)
        # sing that voice once - summing a render with itself only makes it louder
        ks = list(dict.fromkeys(vkey(p) for p in perfs))
        ys = []
        for k in ks:
            y = voice_audio(k) * gain.get(k, 1.0)
            if k.endswith("|bv") and not lead_only:   # new way: backing goes in untouched (stereo), not through the chain
                b = stem_audio("backing")
                n0 = min(len(y), len(b))
                y = y[:n0] + b[:n0]
            ys.append(y)
        n = min(len(y) for y in ys)
        if len(ys) == 1:
            y = ys[0][:n]
        else:  # everyone at once: equal-power sum, soft-limited
            y = sum(v[:n] for v in ys) / len(ys) * np.sqrt(len(ys))
        over = np.abs(y) > 0.9
        y[over] = np.sign(y[over]) * (0.9 + 0.1 * np.tanh((np.abs(y[over]) - 0.9) / 0.1))
        safe = re.sub(r"[^\w+ -]", "_", key)[:80]
        path = os.path.join(takes_dir, f"{safe}.wav")
        sf.write(path, y, target_sr, subtype="FLOAT")
        take_paths[key] = path
        return path

    sections = [{"start": s, "vocal": take_for(perfs)} for s, _, perfs in spans]
    sections[0]["start"] = 0
    save(pdir, "sections.json", sections)
    if len(sections) == 1:
        # one voice for the whole song (skits, solo tracks): nothing to splice -
        # splice_stage needs 2+ sections, and the single take is already full length
        spliced = sections[0]["vocal"]
        log("One section - no splicing needed.")
    else:
        spliced = os.path.join(pdir, "spliced.wav")
        log(f"Splicing {len(sections)} sections...")
        subprocess.run([sys.executable, os.path.join(PIPELINE_DIR, "splice_stage.py"),
                        "--original", vocals, "--sections", os.path.join(pdir, "sections.json"),
                        "--output", spliced, "--snap-window", "0"], check=True)
    # Width: the mixer centres every vocal layer (RVC is mono), but the original backing vocals carry most
    # of a song's stereo (Monster: 92% of the vocal stem's side energy, mix 3.4 dB narrower than the original,
    # 2026-10-23: "feels more wide open in the original"). Add the backing stem's side signal back
    # in the "keep backing" sections; its mid is already in the spliced vocal.
    side_path = None
    backing_path = None      # new way: the original backing (stereo), gated to the keep-backing sections
    ref_vocals = vocals
    if new_way and split.get("backing") and any(vkey(p).endswith("|bv") for _, _, ps in spans for p in ps):
        bs, bsr = backing_track()
        mask = np.zeros(len(bs))
        for s0, e0, perfs in spans:
            if any(vkey(p).endswith("|bv") for p in perfs):
                mask[int(s0 * bsr):int(e0 * bsr)] = 1.0
        ramp = int(0.02 * bsr)
        mask = np.convolve(mask, np.ones(ramp) / ramp, mode="same")
        backing_path = os.path.join(pdir, "backing_gated.wav")
        sf.write(backing_path, bs * mask[:, None], bsr, subtype="FLOAT")
        ref_vocals = split["lead"]           # the lead was converted without its backing
        log(f"  new way: {'converted' if conv_backing else 'original'} backing in the keep-backing sections")
    elif split.get("backing") and not lead_only and not conv_backing:   # a converted (mono) backing has no width to restore
        bs, bsr = sf.read(split["backing"], always_2d=True)
        if bs.shape[1] == 2:
            mask = np.zeros(len(bs))
            ramp = int(0.02 * bsr)
            for s0, e0, perfs in spans:
                if any(vkey(p).endswith("|bv") for p in perfs):
                    mask[int(s0 * bsr):int(e0 * bsr)] = 1.0
            if mask.any():
                mask = np.convolve(mask, np.ones(ramp) / ramp, mode="same") if ramp > 1 else mask
                side = (bs[:, 0] - bs[:, 1]) / 2 * mask
                side_path = os.path.join(pdir, "backing_side.wav")
                sf.write(side_path, np.column_stack((side, -side)), bsr, subtype="FLOAT")
                log("  backing width restored (side signal in keep-backing sections)")
    out = proj["output_path"]
    artist = " & ".join(dict.fromkeys(
        ("Original" if casting[p]["voice"] == ORIGINAL else casting[p]["voice"].rsplit(".", 1)[0])
        for p in proj["parts"]["performers"] if p in casting))
    # Original Artist tag: the source file's own artist tag, else the artist from the Genius page
    original_artist = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=artist", "-of", "default=nw=1:nk=1",
         proj.get("song", "")], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ).stdout.strip() or proj.get("artist", "")
    log("Mixing with the instrumental...")
    subprocess.run([sys.executable if os.name == "nt" else "python3", os.path.join(PIPELINE_DIR, "combine_stage.py"),
                    "--vocals", spliced, "--instrumental", proj["stems"]["instrumental"],
                    "--reference-vocals", ref_vocals, "--original", proj.get("song", ""),
                    "--output", out, "--artist", artist, "--title", proj.get("title") or proj["name"]]
                   + (["--original-artist", original_artist] if original_artist else [])
                   + (["--stereo-extra", side_path] if side_path else [])
                   + (["--backing-vocals", backing_path] if backing_path else [])
                   # VOCAL_REVERB=8 (percent) from the job runner adds the short vocal reverb
                   + (["--vocal-reverb", os.environ["VOCAL_REVERB"]] if os.environ.get("VOCAL_REVERB") else []),
                   check=True)
    log(f"Done: {out}")


if __name__ == "__main__":
    cmd, pdir = sys.argv[1], sys.argv[2]
    {"analyze": analyze, "build": build}[cmd](pdir)
