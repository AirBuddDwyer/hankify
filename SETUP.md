# Setting up Hankifier + Hankiplier

This reproduces the working setup: Hankify drives **UVR5** (stem separation) and **RVC** (voice conversion) as
separate programs. Each has its own Python venv because their PyTorch versions conflict. This repo is the glue.

**Windows:** no manual setup. Build the package with `windows/make_zip.sh OUT.zip` (never bundles voices; users get them from voice-models.com); the recipient
unzips it and double-clicks `Install Hankify.bat` (see `README - START HERE.txt`). It runs `windows/install.ps1`, which
puts uv, Python 3.12, PyTorch, the pinned packages (`windows/requirements-windows.txt`), RVC-WebUI at the commit below with
the patch pre-applied (`windows/rvc-overlay/`), ffmpeg and the models inside that folder, and uses
[python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) (`"separator": "audio-separator"`) with
the same two UVR models instead of a UVR checkout.

**Linux, quick way:** `./install_linux.sh` does the same thing on Linux (one venv in this folder, uv + Python 3.12,
PyTorch for your card or CPU, the pinned packages in `linux/requirements-linux.txt`, RVC-WebUI at the commit below with the
same patched files as Windows, audio-separator instead of a UVR checkout, the models, and a `config.json` in this folder
that `run_hankify.sh` / `run_hankiplier.sh` pick up). `--help` lists the options.

Everything below is the **manual** Linux setup (advanced: two venvs, the real UVR5 GUI as the separator).

Tested on: Linux (Arch), NVIDIA GTX 1650 4 GB, driver 615, Python 3.12 (RVC) and 3.10 (UVR), ffmpeg 9.
Exact package lists from the working machine are in `reference/*-freeze.txt` (use them as a guide, not blindly:
the UVR one has CUDA 13 wheels that only suit recent NVIDIA drivers).

## 0. System packages
```bash
sudo apt install -y python3.12 python3.12-venv python3.12-dev python3.10 python3.10-venv python3-tk ffmpeg libsndfile1 git
```
(Names are for Debian/Ubuntu; use your distro's equivalents. Tkinter must be available to the **UVR** venv's Python.)

## 1. Clone this repo
```bash
git clone <this repo's URL> hankifier
```

## 2. RVC-WebUI (voice conversion)
Pinned to a known-good upstream commit, plus a small patch (`patches/rvc-webui.patch`: frees the model on unload, makes
single-inference autosave opt-in via `RVC_AUTOSAVE_DIR`, and adds the optional `RVC_F0_CENTER` octave-fold env var
used to keep a voice on one melody line when a stem holds two singers an octave apart).
```bash
git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git ~/RVC-WebUI
cd ~/RVC-WebUI
git checkout 81eed5e
git apply /path/to/hankifier/patches/rvc-webui.patch

python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
# NVIDIA before RTX 50-series (CUDA 11.8); for RTX 50 / CPU use the other requirments_*.txt, see RVC's README
python -m pip install torch==2.7.1+cu118 torchaudio==2.7.1+cu118 \
  --index-url https://download.pytorch.org/whl/cu118 --extra-index-url https://pypi.org/simple
python -m pip install -r requirments_cu118_py312.txt
# extras Hankiplier's engine uses (Whisper word timestamps, singer matching):
python -m pip install openai-whisper librosa scikit-learn scipy
```
Base models (once):
```bash
python -m pip install --upgrade huggingface_hub
hf download lj1995/VoiceConversionWebUI --revision main --include "hubert_base/*" --local-dir assets
hf download lj1995/VoiceConversionWebUI rmvpe.pt --revision main --local-dir assets/rmvpe
```
**Voice models:** put each character's `.pth` in `assets/weights/` and its `.index` in `assets/indices/` (same name stem).
Hankify lists whatever `.pth` files are in `assets/weights`. Models are not included in this repo. A model's name must not
be a substring of another model's index name (the index is matched by substring).

## 3. Ultimate Vocal Remover (stem separation)
Also pinned, with a patch (`patches/uvr.patch`) that lets `song-pipeline/uvr_stage.py` drive UVR's window headlessly:
it uses the pip `tkinterdnd2`, makes background threads hand UI updates to the main thread, and adds model definitions.
```bash
git clone https://github.com/Anjok07/ultimatevocalremovergui.git ~/ultimatevocalremovergui
cd ~/ultimatevocalremovergui
git checkout 5517e0c
git apply /path/to/hankifier/patches/uvr.patch

python3.10 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
# My working venv, for reference: reference/uvr-venv-freeze.txt (torch 2.14 with CUDA 13 wheels). If your driver is
# older, install the torch build that matches it from pytorch.org first and then the rest of that file minus the
# torch/nvidia lines. Whatever you do, this venv needs UVR's dependencies plus:
python -m pip install tkinterdnd2 Pillow    # also needed by the Hankify/Hankiplier GUIs, which run in this venv
```
**Pick the models once, in UVR's own GUI** (`python UVR.py`): `uvr_stage.py` reuses whatever UVR last saved.
1. Settings → Download Center → MDX-Net: download **MDX23C-InstVoc HQ** and **UVR-MDX-NET Karaoke 2** (the second is only used for "preserve backing vocals").
2. Process method **MDX-Net**, model **MDX23C-InstVoc HQ**, "GPU Conversion" ticked, segment size 256. Close UVR.

Optional speed-up (my setup): UVR's torch is built for CUDA 13 but its `onnxruntime-gpu` wants CUDA 12, so the ONNX Karaoke
model silently ran on CPU. Installing the CUDA 12 runtime libs into `<uvr dir>/cuda12-libs` (via
`pip install --target cuda12-libs nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cuda-nvrtc-cu12`)
makes `uvr_stage.py` preload them (~3 min -> ~18 s). If the folder is absent it is skipped.

## 4. Config
Paths are not hardcoded. Copy `config.example.json` to `~/.config/hankifier/config.json` and edit it, or use environment
variables (`HANKIFIER_RVC_DIR`, `HANKIFIER_UVR_DIR`, `HANKIFIER_OUTPUT_DIR`, `HANKIFIER_RUNS_DIR`, ...; they win over the file).
```bash
python3 song-pipeline/hankify_config.py show     # prints what everything resolved to
```
| key | default | meaning |
| --- | --- | --- |
| `rvc_dir` | `~/RVC-WebUI` | RVC checkout (patched) |
| `rvc_python` | `<rvc_dir>/.venv/bin/python` | its venv |
| `uvr_dir` | `~/ultimatevocalremovergui` | UVR checkout (patched) |
| `uvr_python` | `<uvr_dir>/.venv/bin/python` | its venv; also runs the GUIs |
| `output_dir` | `~/Music/hankify` | finished MP3/MP4 files |
| `runs_dir` | `~/.cache/hankifier/pipeline_runs` | per-run scratch (stems); deleted after a run unless "keep intermediates" |
| `intermediates_dir` | `<output_dir>/intermediates` | kept stems (Hankiplier reads these) |
| `fallback_image` | none | still used for MP4s when you pick no image |
| `hankifier_dir` | the repo root | only for Hankiplier, if it is run from somewhere other than this repo's `hankiplier/` folder |

## 5. Run it
```bash
./run_hankify.sh                      # the GUI: drag songs in, pick a voice, Start
python3 song-pipeline/pipeline.py "song.mp3" --voice "My Voice.pth"      # or the CLI, no GUI
```
`desktop/hankify.desktop.template` is a launcher entry (replace `@REPO@` with the repo path).

### Hankiplier (multi-voice)
```bash
./run_hankiplier.sh                   # the multi-voice GUI
```
It runs in the UVR venv like Hankify (tkinterdnd2, Pillow); its engine (`hankiplier/engine.py`: Whisper word timings,
singer matching, cut building) runs in the RVC venv, which needs the extras from step 2 (`openai-whisper librosa
scikit-learn scipy`). Projects are saved in `hankiplier/projects/` (git-ignored). It reads the kept stems from
`intermediates_dir`. `desktop/hankiplier.desktop.template` is its launcher entry. The Windows package does not include
Hankiplier yet. Env vars: `HANKIPLIER_LANG=es` (or another Whisper language code) for songs not in English,
`HANKIPLIER_CPU=1` to keep the analysis off the GPU.

## Notes
- **4 GB VRAM is enough** but only one GPU job at a time; the pipeline kills leftover UVR/RVC processes between stages.
- Defaults that matter (UVR settings above, RVC pitch 0, index rate ~0.95) are explained in
  `docs/Hankify - How It Works.pdf`.
- Settings and logs (`hankify/config.json`, `errors.log`) are created at runtime and git-ignored.
- **De-esser.** The new-way mixer (`combine_new.py`) uses ffmpeg's `deesser` filter. An optional split-band de-esser
  module can be plugged in by pointing env `RVC_DEESSER_PATH` at a folder with a `deesser.py` exposing `Settings` and
  `process`; without it (the normal case) the ffmpeg filter is used automatically. `--deesser ffmpeg` (or env
  `VOCAL_DEESSER=ffmpeg`) forces the filter.
- **Waves Vocal Rider is optional.** Without it, both mixers use the built-in rider (`song-pipeline/vocal_rider.py`,
  numpy/soundfile from the RVC venv) automatically; nothing to configure. The Waves path is only used when
  `~/.local/share/vocal-rider-host/.venv/bin/python` exists (a venv with `pedalboard`) and the Waves plugin is loadable
  through yabridge at `~/.vst3/yabridge/WaveShell1-VST3 17.1_x64.vst3`. If that host is missing, fails or hangs, the
  render falls back to the built-in rider and keeps going. The mix report shows which one ran (`"rider": "waves +-3"` or
  `"native +-3"`).
