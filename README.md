# Hankifier

RVC voice conversion suite: drop in a song, pick a character voice, get the song sung by that character.

- `hankify/app.py`: Tkinter drag-and-drop GUI (needs `tkinterdnd2`, `Pillow`).
- `song-pipeline/`: the CLI pipeline the GUI drives. UVR5 stem separation (`uvr_stage.py`) -> RVC conversion
  (`rvc_stage.py`) -> vocal FX + ffmpeg mix (`combine_stage.py`, presets in `vocal_presets.json`).
  Run directly with `python3 pipeline.py <song> --voice <model.pth>`.

Mastering chapter (the default "new way" mixer, combine_new.py, and the old preset chain): [docs/Mastering.md](docs/Mastering.md).

## Windows
Download the repo (Code > Download ZIP), unzip it to `C:\Hankify`, and follow **[README - START HERE.txt](README%20-%20START%20HERE.txt)**: double-click `Install Hankify.bat`, then `Hankify.bat`. No voice models are included: find them on [voice-models.com](https://voice-models.com) (or google "<character> RVC model") and add the downloaded `.zip` with **Import Voice...** in the app. Full steps: GETTING VOICES in the START HERE file.

## Setup
See **[SETUP.md](SETUP.md)** for the full walkthrough: installing RVC-WebUI and UVR5 at pinned commits (with the small
patches in `patches/`), picking the UVR models, and writing `~/.config/hankifier/config.json`.
Paths are configuration, not code: `python3 song-pipeline/hankify_config.py show` prints what is in effect.

Run: `./run_hankify.sh` (GUI) or `python3 song-pipeline/pipeline.py song.mp3 --voice "Model.pth"` (CLI).

## Open problems
- **Pitch shifting (transpose) and index rate.** Shifting pitch for parts outside a character's range (e.g. a female vocal
  through a male model) has not been solved. Nothing here is a validated rule: the PDF guide says to drop the index rate to
  0.2 whenever pitch is not 0, but that has not been confirmed to help; the GUI defaults to 0.95 and the CLI to 0.75.
  Treat pitch + index rate as something to experiment with per song (render a couple of variants and listen).
  Experiments and results are welcome.

## Built on
Hankifier is glue code around these open-source projects, which are installed separately and keep their own licenses:
- [Ultimate Vocal Remover (UVR5)](https://github.com/Anjok07/ultimatevocalremovergui): stem separation (MDX23C, MDX-Net Karaoke).
- [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI): voice conversion, with RMVPE pitch tracking.
- [FFmpeg](https://ffmpeg.org/): mixing, loudness and MP3/MP4 export.
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) and [Pillow](https://python-pillow.org/): GUI.

The modified RVC files in `windows/rvc-overlay/` and the patches keep their upstream notices: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Voice models, songs and character art are not included and belong to their respective owners.
