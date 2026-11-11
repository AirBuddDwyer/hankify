"""Quick post-install check: every piece Hankify and Hankiplier need imports and is where the config says.
Prints one line per check and exits non-zero if any fail. Doesn't render anything."""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "song-pipeline"))
import hankify_config as cfg  # noqa: E402

failed = []


def check(label, fn):
    try:
        note = fn()
        print(f"    ok    {label}" + (f": {note}" if note else ""))
    except Exception as e:  # noqa: BLE001 - report everything, keep going
        print(f"    FAIL  {label}: {e}")
        failed.append(label)


def torch_info():
    import torch
    if torch.cuda.is_available():
        return f"{torch.__version__}, GPU {torch.cuda.get_device_name(0)}"
    return f"{torch.__version__}, no GPU (CPU mode)"


def tools():
    for t in ("ffmpeg", "ffprobe"):
        if not shutil.which(t):
            raise RuntimeError(f"{t} not found")
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libmp3lame" not in out:
        raise RuntimeError("this ffmpeg has no MP3 encoder (libmp3lame)")


def rvc():
    here = os.getcwd()
    os.chdir(cfg.RVC_DIR)
    sys.path.insert(0, cfg.RVC_DIR)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        from configs.config import Config  # noqa: F401
        from infer.vc.modules import VC  # noqa: F401
    finally:
        sys.argv = argv
        os.chdir(here)
    for f in ("assets/hubert_base/pytorch_model.bin", "assets/rmvpe/rmvpe.pt"):
        if not os.path.isfile(os.path.join(cfg.RVC_DIR, f)):
            raise RuntimeError(f"missing {f}")


def separator():
    import audio_separator  # noqa: F401
    import uvr_stage
    for m in (uvr_stage.SEPARATOR_MAIN_MODEL, uvr_stage.SEPARATOR_SPLIT_MODEL):
        if not os.path.isfile(os.path.join(cfg.SEPARATOR_MODELS_DIR, m)):
            raise RuntimeError(f"missing model {m}")


def gui():
    import tkinter  # noqa: F401
    import tkinterdnd2  # noqa: F401
    import PIL  # noqa: F401


def multivoice():
    """Hankiplier's extra pieces: Whisper (lyrics timing) and scikit-learn (telling the singers apart)."""
    import sklearn  # noqa: F401
    import whisper
    d = os.environ.get("HANKIPLIER_WHISPER_DIR")
    if d:
        size = os.environ.get("HANKIPLIER_WHISPER_MODEL", "small")
        if not os.path.isfile(os.path.join(d, os.path.basename(whisper._MODELS[size]))):
            raise RuntimeError(f"Whisper {size} model missing in {d}")
        return f"Whisper {size} ready"
    return "Whisper downloads its model the first time cuts are detected"


def voices():
    names = [f for f in os.listdir(cfg.RVC_WEIGHTS_DIR) if f.lower().endswith(".pth")]
    if not names:
        return "none yet - add them in Hankify with Import Voice... (see README - START HERE.txt)"
    return f"{len(names)}: " + ", ".join(sorted(os.path.splitext(n)[0] for n in names))


check("PyTorch", torch_info)
check("ffmpeg", tools)
check("RVC voice conversion", rvc)
check("vocal separation", separator)
check("window + drag and drop", gui)
check("multi-voice (Hankiplier)", multivoice)
check("voices", voices)
sys.exit(1 if failed else 0)
