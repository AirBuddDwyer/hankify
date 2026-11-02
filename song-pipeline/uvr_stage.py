#!/usr/bin/env python3
"""Headless UVR5 stem separation.

Must be run with UVR5's own venv python, e.g.:
    <UVR dir>/.venv/bin/python uvr_stage.py \
        --input song.wav --outdir /path/to/per-run/dir

Drives UVR.MainWindow() directly instead of clicking through its GUI (this
needs the patched UVR from patches/uvr.patch). Reuses whatever separation model/settings are currently saved in
the UVR5 GUI (its normal settings-persistence mechanism), except it forces a
concrete primary-stem selection, since the GUI's "All Stems" default only
makes sense for multi-stem models and is normally corrected by a
model-selection callback that this headless path never triggers.

On success, prints {"vocals": "...", "instrumental": "..."} as JSON on stdout -
or, with --split-backing-vocals, {"lead_vocals": "...", "backing_vocals": "...",
"instrumental": "..."} instead of "vocals".
On failure, prints a message to stderr and exits non-zero.
"""
import argparse
import json
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hankify_config  # noqa: E402

UVR_DIR = hankify_config.UVR_DIR

# UVR5's built-in "vocal splitter" feature: after the main Vocals/Instrumental
# separation, it feeds the Vocals stem through a second, karaoke-style model
# that further splits it into a lead-vocal-only stem and a backing-vocals-only
# stem. This is the model that split gets driven with when requested.
VOCAL_SPLIT_MODEL = "MDX-Net: UVR-MDX-NET Karaoke 2"

# UVR's venv has torch built for CUDA 13, but its onnxruntime-gpu (used for
# the MDX-Net .onnx models, e.g. the karaoke splitter) is built for CUDA 12,
# so without these it silently falls back to CPU (~15x slower). They live in
# their own --target dir rather than the venv, because pip's CUDA 12 wheels
# install into the same nvidia/*/lib dirs as torch's CUDA 13 ones and would
# overwrite them. cuDNN is deliberately not in this list: torch has already
# loaded its own libcudnn.so.9 by the time this runs, and onnxruntime reuses
# it (verified bit-for-bit against CPU output).
CUDA12_LIBS_DIR = os.path.join(UVR_DIR, "cuda12-libs", "nvidia")
CUDA12_LIBS = [
    "cuda_runtime/lib/libcudart.so.12",
    "cublas/lib/libcublasLt.so.12",
    "cublas/lib/libcublas.so.12",
    "cufft/lib/libcufft.so.11",
    "curand/lib/libcurand.so.10",
    "cuda_nvrtc/lib/libnvrtc.so.12",
]


def preload_cuda12_for_onnxruntime():
    """Load the CUDA 12 runtime libs globally so onnxruntime's CUDA provider
    can resolve them when it's first used. Must run after torch is imported
    (see CUDA12_LIBS_DIR). Failure just means onnxruntime stays on CPU."""
    import ctypes
    for lib in CUDA12_LIBS:
        try:
            ctypes.CDLL(os.path.join(CUDA12_LIBS_DIR, lib), mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            print(f"uvr_stage: couldn't load {lib} ({e}); ONNX models will run on CPU", file=sys.stderr)
            return


def fail(message):
    print(f"uvr_stage: {message}", file=sys.stderr)
    sys.exit(1)


# The same two models the UVR5 GUI setup uses (SETUP.md), by their audio-separator filenames.
SEPARATOR_MAIN_MODEL = "MDX23C-8KFFT-InstVoc_HQ.ckpt"     # MDX23C-InstVoc HQ
SEPARATOR_SPLIT_MODEL = "UVR_MDXNET_KARA_2.onnx"          # UVR-MDX-NET Karaoke 2


def separate_with_audio_separator(input_path, outdir, split_backing_vocals):
    """The "audio-separator" backend (hankify_config.SEPARATOR): same models as the UVR5 GUI,
    no UVR checkout. Writes stems with UVR's filename endings so pipeline.py's cache finds them,
    and returns the same dict the UVR path prints."""
    import logging
    from audio_separator.separator import Separator

    base = os.path.splitext(os.path.basename(input_path))[0]
    sep = Separator(output_dir=outdir, model_file_dir=hankify_config.SEPARATOR_MODELS_DIR,
                    output_format="WAV", log_level=logging.WARNING,
                    mdxc_params={"segment_size": 256, "overlap": 8, "batch_size": 1,
                                 "override_model_segment_size": False, "pitch_shift": 0})

    def run(model, source, names):
        sep.load_model(model_filename=model)
        out = sep.separate(source, custom_output_names=names)
        paths = [p if os.path.isabs(p) else os.path.join(outdir, p) for p in out]
        found = {}
        for stem, name in names.items():
            match = [p for p in paths if os.path.splitext(os.path.basename(p))[0] == name]
            if len(match) != 1 or not os.path.isfile(match[0]):
                fail(f"{model} did not write the {stem} stem (got {out})")
            found[stem] = match[0]
        return found

    main = run(SEPARATOR_MAIN_MODEL, input_path,
               {"Vocals": f"1_{base}_(Vocals)", "Instrumental": f"1_{base}_(Instrumental)"})
    if not split_backing_vocals:
        return {"vocals": main["Vocals"], "instrumental": main["Instrumental"]}
    # Karaoke model: its "Vocals" stem is the lead, its "Instrumental" stem is the backing vocals.
    split = run(SEPARATOR_SPLIT_MODEL, main["Vocals"],
                {"Vocals": f"2_{base}_(Vocals_lead_only)", "Instrumental": f"2_{base}_(Vocals_backing_only)"})
    return {"lead_vocals": split["Vocals"], "backing_vocals": split["Instrumental"],
            "instrumental": main["Instrumental"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to the input song")
    parser.add_argument("--outdir", required=True, help="Directory to write stems into")
    parser.add_argument("--timeout", type=int, default=1200, help="Max seconds to wait for separation")
    parser.add_argument("--split-backing-vocals", action="store_true",
                         help="Also split the Vocals stem into lead-only and backing-only stems")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    outdir = os.path.abspath(args.outdir)

    if not os.path.isfile(input_path):
        fail(f"input file not found: {input_path}")
    os.makedirs(outdir, exist_ok=True)

    if hankify_config.SEPARATOR == "audio-separator":
        print(json.dumps(separate_with_audio_separator(input_path, outdir, args.split_backing_vocals)))
        return
    if hankify_config.SEPARATOR != "uvr":
        fail(f"unknown separator {hankify_config.SEPARATOR!r} (use \"uvr\" or \"audio-separator\")")

    sys.path.insert(0, UVR_DIR)
    os.chdir(UVR_DIR)
    import torch  # noqa: E402,F401 - loads torch's cuDNN first; see CUDA12_LIBS_DIR
    import UVR  # noqa: E402
    preload_cuda12_for_onnxruntime()

    root = UVR.MainWindow()
    UVR.root = root  # messagebox parent= calls reference this module-level global
    root.withdraw()

    # Headless runs must never block waiting for a click on a window nobody
    # can see. Treat any confirmation dialog as declined; real errors are
    # still fully captured in error_log_var / command_Text below.
    UVR.messagebox.askyesno = lambda *a, **k: False
    UVR.messagebox.showerror = lambda *a, **k: None
    UVR.messagebox.showwarning = lambda *a, **k: None
    UVR.messagebox.showinfo = lambda *a, **k: None

    # A CPU-only run (CUDA hidden) makes UVR untick "GPU Conversion" and save that to data.pkl, which
    # then sends every later run to the CPU. Turn it back on whenever a GPU is actually visible.
    import torch
    if torch.cuda.is_available():
        root.is_gpu_conversion_var.set(True)

    root.inputPaths = [input_path]
    root.export_path_var.set(outdir)
    root.mdxnet_stems_var.set(UVR.VOCAL_STEM)

    if args.split_backing_vocals:
        root.set_vocal_splitter_var.set(VOCAL_SPLIT_MODEL)
        root.is_set_vocal_splitter_var.set(True)
        root.is_save_inst_set_vocal_splitter_var.set(False)
        if root.process_determine_vocal_split_model() is None:
            fail(f"vocal split model not available: {VOCAL_SPLIT_MODEL}")
    else:
        root.is_set_vocal_splitter_var.set(False)

    if not root.process_preliminary_checks():
        fail("no valid separation model is currently selected in UVR5's saved settings")

    root.process_initialize()
    thread = getattr(root, "active_processing_thread", None)
    if thread is None:
        fail("processing did not start (an early validation check failed silently)")

    thread.join(timeout=args.timeout)
    if thread.is_alive():
        fail(f"separation did not finish within {args.timeout}s")

    # No mainloop is running, so drain the thread-safe UI queue ourselves -
    # this is where the real success/failure state (error_log_var, button
    # state, etc.) actually gets applied.
    try:
        while True:
            root.ui_update_queue.get_nowait()()
    except queue.Empty:
        pass

    error_text = root.error_log_var.get().strip()
    if error_text:
        fail(f"UVR5 reported an error:\n{error_text}")

    instrumental = [f for f in os.listdir(outdir) if f.endswith(f"({UVR.INST_STEM}).wav")]
    if len(instrumental) != 1:
        fail(f"expected exactly one Instrumental file in {outdir}, found {instrumental}")

    if args.split_backing_vocals:
        lead = [f for f in os.listdir(outdir) if f.endswith(f"({UVR.VOCAL_STEM}_{UVR.LEAD_VOCAL_STEM}).wav")]
        backing = [f for f in os.listdir(outdir) if f.endswith(f"({UVR.VOCAL_STEM}_{UVR.BV_VOCAL_STEM}).wav")]
        if len(lead) != 1 or len(backing) != 1:
            fail(
                "expected exactly one lead-vocal and one backing-vocal file in "
                f"{outdir}, found lead={lead} backing={backing}"
            )
        result = {
            "lead_vocals": os.path.join(outdir, lead[0]),
            "backing_vocals": os.path.join(outdir, backing[0]),
            "instrumental": os.path.join(outdir, instrumental[0]),
        }
    else:
        vocals = [f for f in os.listdir(outdir) if f.endswith(f"({UVR.VOCAL_STEM}).wav")]
        if len(vocals) != 1:
            fail(f"expected exactly one Vocals file in {outdir}, found {vocals}")
        result = {
            "vocals": os.path.join(outdir, vocals[0]),
            "instrumental": os.path.join(outdir, instrumental[0]),
        }
    root.destroy()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
