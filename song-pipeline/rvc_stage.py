#!/usr/bin/env python3
"""Headless RVC voice conversion.

Must be run with RVC-WebUI's own venv python, e.g.:
    <RVC-WebUI>/.venv/bin/python rvc_stage.py \
        --input vocals.wav --model "My Voice.pth" --output converted.wav

Calls infer.vc.modules.VC directly - no Gradio/browser involved. Defaults
for f0-up-key/f0-method/index-rate/rms-mix-rate/protect match RVC-WebUI's
own inference-tab GUI defaults (webui.py's component definitions).
"""
import argparse
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hankify_config  # noqa: E402

RVC_DIR = hankify_config.RVC_DIR

# RMVPE pitch extraction runs on the whole clip in one pass, and its VRAM use
# scales with audio length. On this 4GB card that OOMs at around 5-6
# minutes of vocals. Converting in chunks, each capped at MAX_CHUNK_SEC
# (padding included), keeps every pass well under that ceiling regardless of
# song length.
MAX_CHUNK_SEC = 270.0

# Where the chunks join (2026-10-23: "I always hear it"): the old
# equal-length split blended two different conversions over a 4 s band in
# the middle of whatever was being sung, which smeared audibly. Each cut now
# goes at the quietest QUIET_WIN_SEC stretch in the back half of the allowed
# chunk length (a real pause, not the gap between two consonants), and only
# JOIN_XFADE_SEC right at the cut is blended.
QUIET_WIN_SEC = 1.0
JOIN_XFADE_SEC = 0.05

# Each chunk still gets this much extra input on both sides of each cut so
# the conversion has real context at its edges; that padding is converted
# and then trimmed back off, never blended.
SPLIT_PAD_SEC = 2.0

# Same defaults webui.py sets via os.environ.setdefault(...) before anything
# else reads them - replicated here since we import the modules directly
# instead of importing webui.py.
os.environ.setdefault("weight_root", "assets/weights")
os.environ.setdefault("index_root", "logs")
os.environ.setdefault("outside_index_root", "assets/indices")
os.environ.setdefault("rmvpe_root", "assets/rmvpe")
# CUDA graph capture (tools/cuda_graph.py) trades a large one-time memory
# pool for faster *repeated* inference in a long-running server. This script
# does one conversion and exits, so that memory pool is pure overhead - and
# on this 4GB card, capture alone was enough to OOM a run that otherwise
# fits comfortably. Disable it.
os.environ.setdefault("RVC_CUDA_GRAPH", "0")


def _nonempty_index(path):
    """True for an existing .index with vectors in it (an empty one is a training leftover)."""
    if not os.path.isfile(path):
        return False
    try:
        import faiss
        return faiss.read_index(path).ntotal > 0
    except Exception:  # noqa: BLE001 - unreadable: let RVC's matcher choose instead
        return False


def fail(message):
    print(f"rvc_stage: {message}", file=sys.stderr)
    sys.exit(1)


def convert_one(vc, input_path, index_path, args, label):
    status, opt = vc.vc_single(
        sid=0,
        input_audio_path=input_path,
        f0_up_key=args.f0_up_key,
        f0_method=args.f0_method,
        file_index=index_path,
        index_rate=args.index_rate,
        resample_sr=0,
        rms_mix_rate=args.rms_mix_rate,
        protect=args.protect,
        autosave=False,
    )
    if opt is None or opt[0] is None or opt[1] is None:
        fail(f"conversion failed ({label}): {status}")
    return opt  # (tgt_sr, audio)


def crossfade_concat(audio1, audio2, overlap):
    """Concatenate two arrays that share `overlap` samples of the same
    underlying audio at their join, blending that shared span instead of
    cutting hard at the midpoint.

    vc_single returns int16 PCM, not normalized float - the blend has to be
    done in float and cast back to the original integer dtype/range before
    concatenating, or the mixed-dtype result silently upcasts the *whole*
    array to float while keeping int16-scale magnitudes, which soundfile
    then writes out as if it were normalized float audio (massive clipping).
    """
    overlap = min(overlap, len(audio1), len(audio2))
    if overlap <= 0:
        return np.concatenate([audio1, audio2])
    dtype = audio1.dtype
    # Equal-power (constant-power) curves rather than linear: fade_out**2 +
    # fade_in**2 == 1 at every point, so the blended midpoint doesn't dip in
    # perceived loudness the way a linear ramp does.
    theta = np.linspace(0.0, np.pi / 2, overlap)
    fade_out = np.cos(theta)
    fade_in = np.sin(theta)
    blended = audio1[-overlap:].astype(np.float64) * fade_out + audio2[:overlap].astype(np.float64) * fade_in
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        blended = np.clip(blended, info.min, info.max)
    blended = blended.astype(dtype)
    return np.concatenate([audio1[:-overlap], blended, audio2[overlap:]])


def find_cuts(data, sr):
    """Sample positions to split `data` at so no chunk (padding included)
    exceeds MAX_CHUNK_SEC, each placed at the quietest QUIET_WIN_SEC window
    between half and all of the allowed length after the previous cut."""
    total = len(data)
    if total <= int(MAX_CHUNK_SEC * sr):
        return []
    longest = int((MAX_CHUNK_SEC - 2 * SPLIT_PAD_SEC) * sr)
    mono = np.abs(data).mean(axis=1) if data.ndim > 1 else np.abs(data)
    hop = max(1, int(0.01 * sr))
    frames = len(mono) // hop
    energy = (mono[: frames * hop].astype(np.float64) ** 2).reshape(frames, hop).mean(axis=1)
    win = max(1, int(round(QUIET_WIN_SEC / 0.01)))
    smooth = np.convolve(energy, np.ones(win) / win, mode="same")
    cuts, prev = [], 0
    while total - prev > longest:
        lo, hi = (prev + longest // 2) // hop, (prev + longest) // hop
        best = lo + int(np.argmin(smooth[lo:hi]))
        # then the quietest 10 ms inside that second (a breath or a gap
        # between words, when the song never fully pauses)
        a, b = max(lo, best - win // 2), min(hi, best + win // 2 + 1)
        cut = (a + int(np.argmin(energy[a:b]))) * hop
        cuts.append(cut)
        prev = cut
    return cuts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to the vocal stem to convert")
    parser.add_argument("--model", required=True, help="Model filename in assets/weights, e.g. My Voice.pth")
    parser.add_argument("--output", required=True, help="Path to write the converted vocal wav to")
    parser.add_argument("--f0-up-key", type=int, default=0)
    parser.add_argument("--index", help="explicit .index path (e.g. another voice's index); default = the model's own")
    parser.add_argument("--f0-method", default="rmvpe", choices=["pm", "rmvpe", "fcpe"])
    parser.add_argument("--index-rate", type=float, default=0.75)
    parser.add_argument("--rms-mix-rate", type=float, default=0.25)
    parser.add_argument("--protect", type=float, default=0.33)
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    output_path = os.path.abspath(args.output)
    if not os.path.isfile(input_path):
        fail(f"input file not found: {input_path}")

    os.chdir(RVC_DIR)
    sys.path.insert(0, RVC_DIR)

    # Config() parses sys.argv with its own argparse definitions (--port,
    # --pycmd, etc.) - hide our own CLI args from it so it doesn't choke on
    # unrecognized ones.
    real_argv = sys.argv
    sys.argv = [real_argv[0]]
    from configs.config import Config  # noqa: E402
    config = Config()
    sys.argv = real_argv

    from infer.vc.modules import VC  # noqa: E402
    from infer.vc.utils import get_index_path_from_model  # noqa: E402
    import soundfile as sf  # noqa: E402

    weight_root = os.getenv("weight_root")
    model_path = os.path.join(weight_root, args.model)
    if not os.path.isfile(model_path):
        fail(f"model not found: {model_path}")

    vc = VC(config)
    vc.get_vc(args.model)
    if vc.net_g is None:
        fail(f"failed to load model {args.model} (net_g is still None after get_vc)")

    # An index named exactly like the voice (what Hankify's "Import Voice" writes) wins; RVC's own matcher
    # goes by substring and newest file, so "Alice" could otherwise pick up "Alice Singing.index".
    exact = os.path.join(os.environ["outside_index_root"], os.path.splitext(args.model)[0] + ".index")
    if args.index:   # explicit override, e.g. to try one voice's model with another voice's index
        index_path = os.path.abspath(args.index)
    else:
        index_path = exact if _nonempty_index(exact) else get_index_path_from_model(args.model, 0)
    if index_path:
        # An untrained/empty .index (0 vectors) makes RVC crash mid-conversion; convert without it instead.
        try:
            import faiss
            if faiss.read_index(index_path).ntotal == 0:
                print(f"rvc_stage: {os.path.basename(index_path)} is empty, converting without an index", file=sys.stderr)
                index_path = ""
        except Exception as e:  # noqa: BLE001 - an unreadable index is RVC's problem to report
            print(f"rvc_stage: couldn't check {index_path}: {e}", file=sys.stderr)

    data, sr = sf.read(input_path)
    total = len(data)
    bounds = [0] + find_cuts(data, sr) + [total]
    num_parts = len(bounds) - 1
    if num_parts > 1:
        print("rvc_stage: chunk cuts at " + ", ".join(f"{b / sr:.1f}s" for b in bounds[1:-1]), file=sys.stderr)

    pad = int(SPLIT_PAD_SEC * sr)
    parts, pads = [], []
    for i in range(num_parts):
        start = max(0, bounds[i] - (pad if i > 0 else 0))
        end = min(total, bounds[i + 1] + (pad if i < num_parts - 1 else 0))
        parts.append(data[start:end])
        pads.append((bounds[i] - start, end - bounds[i + 1]))

    tgt_sr = None
    audios = []
    for i, part in enumerate(parts):
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            sf.write(tmp.name, part, sr)
            this_sr, audio = convert_one(vc, tmp.name, index_path, args, f"part {i + 1}/{num_parts}")
        finally:
            os.unlink(tmp.name)
        if tgt_sr is None:
            tgt_sr = this_sr
        elif this_sr != tgt_sr:
            fail(f"part {i + 1}/{num_parts} came back at a different sample rate ({this_sr} vs {tgt_sr})")
        audios.append(audio)

    # Trim each chunk's context padding back off, keeping half a crossfade
    # past each cut, then blend just that sliver at the (quiet) cut.
    half = round(JOIN_XFADE_SEC / 2 * tgt_sr)
    trimmed = []
    for i, (audio, (pad_l, pad_r)) in enumerate(zip(audios, pads)):
        lo = round(pad_l / sr * tgt_sr) - (half if i > 0 else 0)
        hi = len(audio) - round(pad_r / sr * tgt_sr) + (half if i < num_parts - 1 else 0)
        trimmed.append(audio[max(0, lo) : min(len(audio), hi)])
    audio_opt = trimmed[0]
    for audio in trimmed[1:]:
        audio_opt = crossfade_concat(audio_opt, audio, 2 * half)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    sf.write(output_path, audio_opt, tgt_sr)
    print(output_path)


if __name__ == "__main__":
    main()
