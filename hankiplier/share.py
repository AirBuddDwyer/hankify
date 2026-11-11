#!/usr/bin/env python3
"""Portable Hankiplier setups: a zip named after the song, readable by people and by other agents.

    <Song Name> - Hankiplier setup.zip
      <Song Name>/
        README.md     what this is, who sings what, the cut table, how to use it
        setup.json    the machine-readable source of truth (what Hankiplier imports)
        lyrics.txt    the lyrics with section headers ([Verse 1: Jay-Z & Kanye])
        cuts.csv      every cut: start time, performers, voice, first lyric
        casting.json  performer -> {voice, pitch, backing}

It never contains audio, renders or machine paths: the recipient needs their own copy of the song. The
setup records the song's duration and a hash of the audio file so the importer can warn when their copy
is a different edit (cuts are timestamps and would be off).

Command line (no GUI needed):
    python3 share.py export PROJECT_DIR [-o OUT.zip]     bundle a project from projects/
    python3 share.py info   BUNDLE                       print the README
    python3 share.py import BUNDLE SONG_FILE [--projects-dir DIR]   create the project for SONG_FILE
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile

FORMAT = "hankiplier-setup"
VERSION = 1
SUFFIX = " - Hankiplier setup.zip"
JSON_EXTENSION = ".hankiplier.json"
DURATION_TOLERANCE = 0.25  # seconds; a different edit/encode that drifts more than this shifts the cuts
ORIGINAL = "__original__"
ORIGINAL_LABEL = "Original vocal (untouched)"
HERE = os.path.dirname(os.path.realpath(__file__))


class ShareError(ValueError):
    pass


def slug(text):  # same rule Hankiplier uses for project folder names
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[^\w .,()&'!+-]+", "_", text).strip()[:80] or "song"


def fmt_time(t):
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:05.2f}"


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_duration(path):
    """Duration in seconds via ffprobe, or None."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _short(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def voice_label(v):
    return ORIGINAL_LABEL if v == ORIGINAL else (v[:-4] if v.lower().endswith(".pth") else v)


# ------------------------------------------------------------------ build
def build_share(proj, plan, song_path=None, name=None):
    """Make the share dict from a project dict, its cuts (plan) and optionally the local song file."""
    if not proj.get("parts"):
        raise ShareError("This project has no lyrics/parts yet - nothing to share.")
    plan = plan if plan and plan.get("parts_hash") == proj.get("parts_hash") else None
    song = {"name": name or proj.get("name") or "", "duration": None, "audio_sha1": None}
    if song_path and os.path.isfile(song_path):
        song["duration"] = audio_duration(song_path)
        song["audio_sha1"] = sha1_file(song_path)
    if song["duration"] is None and plan:
        song["duration"] = plan.get("duration")
    casting = {}
    for who, c in (proj.get("casting") or {}).items():
        if isinstance(c, dict) and c.get("voice"):
            casting[who] = {"voice": c["voice"], "pitch": c.get("pitch", 0), "backing": bool(c.get("backing"))}
    return {
        "format": FORMAT, "version": VERSION, "song": song,
        "lyrics": {"text": proj.get("lyrics_text", ""), "genius_url": proj.get("genius_url", ""),
                   "default_singer": proj.get("default_singer", ""),
                   "parts": proj["parts"], "parts_hash": proj.get("parts_hash")},
        "cuts": ({"duration": plan.get("duration"), "sections": plan["sections"],
                  "parts_hash": plan.get("parts_hash"), "warnings": plan.get("warnings", [])} if plan else None),
        "casting": casting,
    }


def cuts_rows(share):
    cuts, cast = share.get("cuts"), share.get("casting") or {}
    rows = []
    for i, s in enumerate((cuts or {}).get("sections", [])):
        voices = " + ".join(voice_label(cast[p]["voice"]) if p in cast else "?" for p in s["performers"])
        rows.append({"n": i + 1, "start_seconds": round(float(s["start"]), 3), "start": fmt_time(s["start"]),
                     "performers": " + ".join(s["performers"]), "voice": voices,
                     "ad_lib": bool(s.get("adlib")), "needs_check": not s.get("confident", True) and not s.get("edited"),
                     "first_lyric": _short(s.get("lyric") or "", 100)})
    return rows


def lyrics_txt(share):
    return (share["lyrics"].get("text") or "").strip() + "\n"


def cuts_csv(share):
    rows = cuts_rows(share)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["n", "start_seconds", "start", "performers", "voice", "ad_lib", "needs_check",
                                         "first_lyric"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def readme_md(share, folder):
    song, lyr, cuts, cast = share["song"], share["lyrics"], share.get("cuts"), share.get("casting") or {}
    counts = (lyr.get("parts") or {}).get("counts") or {}
    performers = (lyr.get("parts") or {}).get("performers") or list(counts)
    L = [f"# {song.get('name') or 'Untitled'}: Hankiplier setup", "",
         "A multi-voice [Hankiplier](https://github.com/AirBuddDwyer/hankify) setup for ONE song: the lyrics, who sings each "
         "part, where every cut is, and which character was cast for each performer. **It contains no audio**: you need "
         "your own copy of the song.", "", "## The song", ""]
    L.append(f"- Name: {song.get('name')}")
    if song.get("duration"):
        L.append(f"- Length of the version the cuts were made on: {fmt_time(song['duration'])} ({song['duration']:.2f} s)")
    if song.get("audio_sha1"):
        L.append(f"- SHA-1 of that audio file: `{song['audio_sha1']}`")
    if lyr.get("genius_url"):
        L.append(f"- Lyrics source: {lyr['genius_url']}")
    L += ["", "## Performers and casting", "", "| Performer | Sections | Character voice | Pitch | Keeps original backing vocals |",
          "| --- | --- | --- | --- | --- |"]
    for p in performers:
        c = cast.get(p)
        L.append(f"| {p} | {counts.get(p, '')} | {voice_label(c['voice']) if c else '(not cast)'} | "
                 f"{c['pitch'] if c else ''} | {'yes' if c and c.get('backing') else 'no'} |")
    L += ["", "Voice names are RVC model file names (without `.pth`); you need the same models installed to reproduce "
          "the casting, or cast different characters.", "", "## Cuts", ""]
    rows = cuts_rows(share)
    if not rows:
        L.append("No cuts were saved with this setup (the lyrics and parts are included; detect the cuts in Hankiplier).")
    else:
        L += ["`start` is the time in the song where the next performer takes over. `check` = the detector was not "
              "confident about that cut; listen to it. Full table with exact seconds: `cuts.csv`.", "",
              "| # | start | performers | voice | first lyric | note |", "| --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            note = ", ".join(x for x, on in (("ad-lib", r["ad_lib"]), ("check", r["needs_check"])) if on)
            L.append(f"| {r['n']} | {r['start']} | {r['performers']} | {r['voice']} | "
                     f"{_short(r['first_lyric'], 60).replace('|', '/')} | {note} |")
    L += ["", "## How to use it", "",
          "**In Hankiplier:** open the song (drag it in), then File > Import setup... and pick this zip. It fills in the lyrics, "
          "parts and cuts, and the casting for every voice you have installed. It warns if your copy of the song has a different "
          "length than the one the cuts were made on (a different edit would put every cut in the wrong place).", "",
          "**From the command line:**", "", "```bash",
          f'python3 share.py import "<this zip>" "/path/to/your/{song.get("name") or "song"}.flac"', "```", "",
          "**If you are an AI agent:** `setup.json` is the source of truth (schema below). `lyrics.txt` is plain text with "
          "`[Section: Performer]` headers, `cuts.csv` is one row per cut, `casting.json` maps performer to voice. Cut times are "
          "seconds from the start of the audio. In lyrics, Genius formatting is kept as markers: `«text»` is an italic line (the "
          "second-listed performer sings it) and `{text}` is a bold line (everyone at once). Verify the user's audio length against `song.duration` in `setup.json` "
          "(tolerance 0.25 s) before trusting the cuts.", "",
          "## Files", "", f"- `{folder}/README.md`: this file", f"- `{folder}/setup.json`: everything below, machine-readable",
          f"- `{folder}/lyrics.txt`: lyrics with section headers", f"- `{folder}/cuts.csv`: the cut table",
          f"- `{folder}/casting.json`: performer -> voice, pitch, keep-backing", "",
          "`setup.json` layout (format `hankiplier-setup`, version 1):", "", "```",
          "song:    {name, duration, audio_sha1}", "lyrics:  {text, genius_url, default_singer, parts, parts_hash}",
          "cuts:    {duration, sections: [{start, performers[], lyric, confident, adlib?}], parts_hash, warnings} | null",
          "casting: {performer: {voice, pitch, backing}}", "```", ""]
    return "\n".join(L)


def write_bundle(path, share):
    """Write the zip; returns its path (a folder name matching the song is used inside)."""
    folder = slug(share["song"].get("name") or "song")
    files = {"README.md": readme_md(share, folder), "setup.json": json.dumps(share, indent=1, ensure_ascii=False),
             "lyrics.txt": lyrics_txt(share), "cuts.csv": cuts_csv(share),
             "casting.json": json.dumps(share.get("casting") or {}, indent=1, ensure_ascii=False)}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(f"{folder}/{name}", text)
    return path


def default_bundle_name(share):
    return slug(share["song"].get("name") or "song") + SUFFIX


# ------------------------------------------------------------------ read / apply
def read_share(path):
    """Read a setup from a bundle zip or a bare setup.json."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.endswith("setup.json") and n.count("/") <= 1]
                if not names:
                    raise ShareError("That zip has no setup.json inside.")
                data = json.loads(z.read(names[0]).decode("utf-8"))
        else:
            with open(path) as f:
                data = json.load(f)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        raise ShareError(f"Couldn't read {path}: {e}")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ShareError("That isn't a Hankiplier setup.")
    if data.get("version", 0) > VERSION:
        raise ShareError(f"This setup is version {data.get('version')}; this Hankiplier reads up to {VERSION}. Update Hankiplier.")
    lyr = data.get("lyrics") or {}
    if not lyr.get("parts") or not isinstance(lyr["parts"].get("lines"), list):
        raise ShareError("The setup has no lyrics/parts.")
    cuts = data.get("cuts")
    if cuts and (not isinstance(cuts.get("sections"), list) or cuts.get("parts_hash") != lyr.get("parts_hash")):
        data["cuts"] = None  # cuts that don't belong to these lyrics are useless
    data.setdefault("casting", {})
    return data


def check_song(share, song_path):
    """Compare the importer's song with the one the cuts were made on.
    Returns (level, message): level is 'exact', 'ok', 'mismatch' or 'unknown'."""
    s = share.get("song") or {}
    if s.get("audio_sha1") and song_path and os.path.isfile(song_path) and sha1_file(song_path) == s["audio_sha1"]:
        return "exact", "Same audio file as the sender's."
    theirs, mine = s.get("duration"), audio_duration(song_path) if song_path else None
    if theirs is None or mine is None:
        return "unknown", "Couldn't compare this song's length with the sender's."
    diff = abs(theirs - mine)
    if diff <= DURATION_TOLERANCE:
        return "ok", f"Different file but the same length ({mine:.1f}s vs {theirs:.1f}s); cuts should line up."
    return "mismatch", (f"This song is {mine:.1f}s long but the cuts were made on a {theirs:.1f}s version "
                        f"(difference {diff:.1f}s). It may be a different edit, so the cuts could be off.")


def usable_casting(share, installed_voices):
    """Split the shared casting into (casting usable here, [(performer, missing voice)]).
    installed_voices=None means 'unknown, keep everything'."""
    have = None if installed_voices is None else set(installed_voices)
    ok, missing = {}, []
    for who, c in (share.get("casting") or {}).items():
        if have is None or c["voice"] == ORIGINAL or c["voice"] in have:
            ok[who] = c
        else:
            missing.append((who, c["voice"]))
    return ok, missing


def apply_to_proj(proj, share, installed_voices):
    """Copy lyrics/parts/casting from the share into a project dict. Returns (plan or None, missing voices)."""
    lyr = share["lyrics"]
    proj["parts"] = lyr["parts"]
    proj["parts_hash"] = lyr.get("parts_hash")
    for k in ("genius_url", "default_singer"):
        proj[k] = lyr.get(k, "")
    proj["lyrics_text"] = lyr.get("text", "")
    proj["casting"], missing = usable_casting(share, installed_voices)
    cuts = share.get("cuts")
    plan = ({"duration": cuts.get("duration"), "sections": cuts["sections"], "parts_hash": cuts["parts_hash"],
             "warnings": cuts.get("warnings", [])} if cuts else None)
    return plan, missing


# ------------------------------------------------------------------ command line
def _installed_voices():
    """Installed RVC voice names, or None when Hankifier's config can't be found."""
    try:
        sys.path.insert(0, HERE)
        import hk_paths
        names = [f for f in os.listdir(hk_paths.cfg.RVC_WEIGHTS_DIR) if f.lower().endswith(".pth")]
        return names
    except (SystemExit, OSError, ImportError):
        return None


def _project_dir(projects_dir, song):
    h = hashlib.sha1(os.path.abspath(song).encode()).hexdigest()[:8]  # same rule as Hankiplier's _project_dir_for
    return os.path.join(projects_dir, f"{slug(os.path.splitext(os.path.basename(song))[0])} [{h}]")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("project_dir"); e.add_argument("-o", "--out")
    i = sub.add_parser("info"); i.add_argument("bundle")
    m = sub.add_parser("import"); m.add_argument("bundle"); m.add_argument("song")
    m.add_argument("--projects-dir", default=os.path.join(HERE, "projects"))
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            def load(n):
                p = os.path.join(a.project_dir, n)
                return json.load(open(p)) if os.path.isfile(p) else None
            proj, plan = load("project.json"), load("plan.json")
            if not proj:
                raise ShareError(f"No project.json in {a.project_dir}")
            song = proj.get("song") if proj.get("song") and os.path.isfile(proj["song"]) else None
            sh = build_share(proj, plan, song)
            out = a.out or os.path.join(os.getcwd(), default_bundle_name(sh))
            print(write_bundle(out, sh))
        elif a.cmd == "info":
            sh = read_share(a.bundle)
            print(readme_md(sh, slug(sh["song"].get("name") or "song")))
        else:
            sh = read_share(a.bundle)
            level, msg = check_song(sh, a.song)
            print(f"song check [{level}]: {msg}")
            pdir = _project_dir(a.projects_dir, a.song)
            pj = os.path.join(pdir, "project.json")
            proj = json.load(open(pj)) if os.path.isfile(pj) else {}
            proj.update({"song": os.path.abspath(a.song), "name": os.path.splitext(os.path.basename(a.song))[0]})
            proj.setdefault("renders", {})
            plan, missing = apply_to_proj(proj, sh, _installed_voices())
            os.makedirs(pdir, exist_ok=True)
            json.dump(proj, open(pj, "w"), indent=1)
            planp = os.path.join(pdir, "plan.json")
            if plan:
                json.dump(plan, open(planp, "w"), indent=1)
            elif os.path.isfile(planp):
                os.remove(planp)
            for who, v in missing:
                print(f"voice not installed, left uncast: {who} -> {voice_label(v)}")
            print(f"project written: {pdir}  (cuts: {len(plan['sections']) if plan else 'none'})")
    except ShareError as err:
        sys.exit(f"share: {err}")


if __name__ == "__main__":
    main()
