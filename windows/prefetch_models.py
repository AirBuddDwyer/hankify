"""Download the models Hankify needs once, at install time, so the first song doesn't stall:
RVC's HuBERT + RMVPE base models (from RVC's official Hugging Face repo) and the two UVR
separation models audio-separator uses. Skips anything already present.

Run with the install's venv python and HANKIFIER_CONFIG pointing at its config.json.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "song-pipeline"))
import hankify_config as cfg  # noqa: E402
import uvr_stage  # noqa: E402

from huggingface_hub import hf_hub_download  # noqa: E402

RVC_BASE_FILES = ["hubert_base/config.json", "hubert_base/preprocessor_config.json",
                  "hubert_base/pytorch_model.bin", "rmvpe.pt"]


def main():
    assets = os.path.join(cfg.RVC_DIR, "assets")
    for name in RVC_BASE_FILES:
        dest = os.path.join(assets, "rmvpe" if name == "rmvpe.pt" else "", name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue
        print(f"    {name}", flush=True)
        hf_hub_download("lj1995/VoiceConversionWebUI", name, revision="main",
                        local_dir=os.path.dirname(dest) if name == "rmvpe.pt" else assets)

    import logging
    from audio_separator.separator import Separator
    os.makedirs(cfg.SEPARATOR_MODELS_DIR, exist_ok=True)
    sep = Separator(model_file_dir=cfg.SEPARATOR_MODELS_DIR, log_level=logging.WARNING)
    for model in (uvr_stage.SEPARATOR_MAIN_MODEL, uvr_stage.SEPARATOR_SPLIT_MODEL):
        print(f"    {model}", flush=True)
        sep.load_model(model_filename=model)     # downloads it the first time


if __name__ == "__main__":
    main()
