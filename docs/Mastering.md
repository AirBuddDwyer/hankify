# Hankify: Mastering and post-processing

This is the "after the voice swap" half of the pipeline: everything that happens to the converted vocal and the finished mix.
It slots into the guide's **"Step 4. Combine"**.

There are two mixers. Since 2026-10-23 the default is the **new way**, `song-pipeline/combine_new.py`, which treats the
converted vocal like a freshly recorded lead and mixes it into the record. The **old way** (`combine_stage.py`'s own preset
chain) is still there and described at the end. `combine_stage.py` is the entry point every renderer calls; when
`vocal_presets.json` has `"mode": "new"` it hands the job to `combine_new.py`.

## Old way vs new way, in one paragraph

The old way processed the vocal with a fixed preset, set its level by **LUFS** against the original vocal stem, added one
generic reverb, summed everything at full gain and normalised to -14 LUFS. Two things went wrong with that. RVC renders the
singer's highs (8 kHz and up) about 9 dB darker than the original voice, so LUFS read the converted vocal as quiet and turned
it up until its mids were 2-5 dB too hot; and the fixed +3 dB @ 3 kHz / +2 dB @ 10 kHz brightening pushed RVC's grit up. The
new way sets the level on the **300 Hz-3 kHz band** where both voices really are, replaces the fixed brightening with a
**dynamic EQ** that only cuts harshness when it happens, **carves room in the instrumental** while the vocal sings, keeps the
original backing vocals **in stereo**, and finishes against the original song. Level matching and the instrumental ducking
do most of the work.

## The new way, in order

```
RVC vocal (mono)
  1. clip gain: active singing to -20 dBFS RMS
  2. corrective EQ: HP 80 Hz, -2 dB @ 280 Hz
  3. de-esser
  4. vocal rider, +-3 dB, slow (Waves Vocal Rider if installed, otherwise the built-in vocal_rider.py; see SETUP.md)
  5. two gentle compressors: 3:1 @ -24 dB (20/100 ms), then 2:1 @ -18 dB (8/60 ms)
  6. dynamic EQ: cut 2.5-4.5 kHz by up to 3 dB only when it rises 2 dB above its usual share of the voice
  7. light saturation: 25% parallel tanh
  8. level: match the ORIGINAL lead vocal in the 300 Hz-3 kHz band (not LUFS)
  9. space: 1.2 s plate + tempo-synced delay (1/4 left, dotted 1/4 right, 2 repeats), both filtered 300 Hz-6 kHz
 10. instrumental ducked 1.5 dB at 2-4 kHz while the vocal sings (linear-phase band split)
 11. mix: vocal + instrumental + original backing vocals in full stereo (or a converted backing voice, level-matched to it)
 12. mix bus: gentle tonal match to the original song (tonal_match.py, +-3 dB), light 1.5:1 bus compression
 13. limiter to -11 LUFS, about -1 dBTP
 14. encode 320 kbps MP3 + ID3 tags
```

Why this order: the vocal is cleaned (EQ, de-esser) before anything reacts to it, so the rider and compressors don't chase
mud and sibilance. Clip gain first means every later threshold means the same thing on every song.

### Controls

| What | How |
| ---- | --- |
| Reverb amount | `--vocal-reverb PCT` = plate percent (delay runs at 60% of it). Default 10% plate / 6% delay. `0` = dry. A `.vocal_reverb` file in a todo folder does the same. |
| Vocal level | `--vocal-gain-db X` / `.vocal_gain_db` / vocalboost's dB all add X on top of the band match. |
| Back to the old way | `COMBINE_WAY=old` for one run, or `"mode": "old"` in `vocal_presets.json`. `--no-vocal-fx`, `--no-loudnorm` or a missing reference vocal also use the old chain. |
| Tonal-match reference | `pipeline.py` passes the original song as `--original`; without it the stems are summed as the reference. |
| Multi-voice (Hankiplier) | build the splice lead-only (`HANKIPLIER_LEAD_ONLY=1 engine.py build <project>`), then run `combine_new.py` on `spliced.wav` with `--backing` = the original backing stem. |

### Reverb rules of thumb

- **No reverb:** rock, country, almost everything before 2010, and anything *exposed* (a cappella, sparse ballads, voice up
  front with little behind it); reverb ruins those.
- **Reverb:** rap and heavily processed pop. When unsure, render both and listen.

### Not automated yet

- De-reverbing the vocal stem before RVC (needs every voice re-rendered).
- Matching the original's actual reverb and delay; it uses a fixed plate + delay.
- Manual clip gain and final volume rides.

## The old way (still available)

`combine_stage.py` with `"mode": "old"`. Every number lives in `vocal_presets.json` (`chain`, `loudness`, `reverb`,
`master_loudness`), plain ffmpeg filters:

```
RVC vocal (mono)
  vocal rider +-6 dB (first)
  HP 80 Hz -> EQ -2.5 dB @ 280 Hz -> compressor 1.5:1 @ -24 dB (15/40 ms, +2 dB)
  -> EQ +3 dB @ 3 kHz -> high shelf +2 dB @ 10 kHz -> compressor 2:1 @ -24 dB (5/20 ms, +1 dB)
  -> de-esser -> high shelf -2 dB @ 8 kHz
  loudness match: gain so the vocal's LUFS = the original vocal stem's LUFS (clamped -6 ... +18 dB)
  short-tail reverb: generated pink-noise IR, 0.8 s, 8% wet
  mix at full gain (amix normalize=0) -> alimiter 0.95 -> two-pass loudnorm -14 LUFS / -1 dBTP / LRA 11
```

`vocal_presets.tonal_hf.json` is a test preset for the old way (tonal match allowed to boost the highs); pass it with
`combine_stage.py --presets FILE`.

## Tags

The ID3 artist is the voice: its model name, or a cleaner display name from the optional `artist_names` map in the
config file (`{"model name without .pth": "Display Name"}`); the original artist is stored in the `TOPE` tag and the title is inherited from the source song.

## Tuning it

- Change one thing at a time and test on the same short song. Describe what you hear and where ("harsh S at 0:42") rather
  than "fix the audio".
- A/B quickly: `COMBINE_WAY=old` vs the default, and `--vocal-reverb 0` vs the default.
- If the vocal sounds dull around 3 kHz, the dynamic EQ is cutting too often (it cut 64% of the time on "Monster"); raise
  its threshold.
- Don't darken the whole chain to fix one song; pass a per-song preset with `--presets FILE` instead.
