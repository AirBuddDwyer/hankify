"""Genius lyrics fetching + performer ("part") identification for Hankiplier.

Stdlib only, so both the GUI (UVR venv) and the engine (RVC venv) can use it.

Marked-lyrics format (what fetch_genius() produces, and what you can type by
hand into the lyrics box):

    [Verse 1: Big Sean & «Kanye West»]      section header, performers listed
    Okay, drop it to the floor              plain line  -> the unmarked performer(s)
    «And niggas still hatin'»               italic line -> the «marked» performer
    {Believe}                               bold line   -> the {marked} performer(s)

Genius convention (user-confirmed): in a section credited to several artists,
italic lines belong to the second-listed artist and bold lines are everyone
at once. When the header itself marks artists («…»/{…}) those marks win.
Parenthesised ad-libs "(…)" and partial-line marks are left to whoever sings
the rest of the line - they overlap the main vocal and can't be cut apart.
"""
import html as htmlmod
import re
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


def fetch_genius(url, timeout=20):
    """Return (marked_lyrics_text, primary_artist, title) for a genius.com URL."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        page = r.read().decode("utf-8", "replace")
    return parse_genius_html(page)


def parse_genius_html(page):
    out = []
    for m in re.finditer(r'<div[^>]*data-lyrics-container="true"[^>]*>', page):
        i = m.end()
        depth = 1
        d = ""
        for t in re.finditer(r"<(/?)div\b[^>]*>", page[i:]):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                d = page[i:i + t.start()]
                break
        d = re.sub(r"<div[^>]*data-exclude-from-selection[^>]*>.*?</div>", "", d, flags=re.S)
        d = re.sub(r"<br\s*/?>", "\n", d)
        d = d.replace("<i>", "«").replace("</i>", "»").replace("<b>", "{").replace("</b>", "}")
        d = re.sub(r"<[^>]+>", "", d)
        out.append(htmlmod.unescape(d))
    text = "\n".join(out).replace("\u00a0", " ").replace("\u200b", "")
    # Genius prepends an "About" blurb before the first header in the first container
    # (one line ending in "Read More") - drop it.
    first = text.find("[")
    if first > 0 and "\n" not in text[:first].strip():
        text = text[first:]
    # ...unless the blurb itself starts with a tag ("[Part 1] Lyrics… Read More [Verse 1: X]"),
    # which glues the real first header onto the blurb line - cut through "Read More".
    line1 = text.split("\n", 1)[0]
    if "Read More" in line1:
        text = text[text.find("Read More") + len("Read More"):].lstrip()
    text = re.sub(r"»«", "", text)          # "«A»«nd x»" -> "«And x»"
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    artist = title = ""
    og = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    if og:
        full = htmlmod.unescape(og.group(1)).replace("\u00a0", " ")
        if " – " in full:
            artist, title = full.rsplit(" – ", 1)
        artist = re.sub(r"\s*\((Ft|Feat)\..*?\)\s*$", "", artist).strip()
    return text, artist, title


SPLIT_RE = re.compile(r"\s*(?:,|&|\+|\band\b|\bx\b|\bwith\b|\bfeat\.?|\bft\.?)\s*", re.I)


def _names(spec):
    """'YB, «Big Sean» & {Fuzzy Jones}' -> [(name, mark)] with mark in '', 'i', 'b'."""
    res = []
    for raw in SPLIT_RE.split(spec):
        raw = raw.strip()
        if not raw:
            continue
        mark = ""
        if raw.startswith("«") or raw.endswith("»"):
            mark = "i"
        elif raw.startswith("{") or raw.endswith("}"):
            mark = "b"
        name = raw.strip("«»{} ").strip()
        if name:
            res.append((name, mark))
    return res


def _section_kind(label):
    return re.sub(r"[\d\s]+$", "", label.strip().lower())


def _line_mark(line):
    """Classify a whole line as '', 'i' or 'b' - only if ALL its non-ad-lib
    words are inside that mark."""
    core = re.sub(r"\([^)]*\)", "", line).strip()
    if not core:
        # A whole line in parentheses is an ad-lib - unless it is formatted,
        # e.g. "(«Yeah, this the real one, baby»)" on All Falls Down: then it is
        # the formatted singer's own line, sung in the gap between the others.
        s = line.strip()
        inner = s[1:-1].strip() if s.startswith("(") and s.endswith(")") else ""
        if re.fullmatch(r"«[^«»]*»[\s,.!?—-]*", inner):
            return "i"
        if re.fullmatch(r"\{[^{}]*\}[\s,.!?—-]*", inner):
            return "b"
        return None  # plain ad-lib line
    if re.fullmatch(r"«[^«»]*»[\s,.!?—-]*", core):
        return "i"
    if re.fullmatch(r"\{[^{}]*\}[\s,.!?—-]*", core):
        return "b"
    return ""


def clean_words(line):
    return re.sub(r"[«»{}()]", " ", line).split()


def spread_marks(text):
    """Genius often wraps several lines in one <i>/<b>, e.g. So Appalled:
        «Five-star dishes, different exotic fishes
        Man, this shit is fuckin' ridiculous
        Fuckin' ridiculous»
    Close an unclosed mark at the end of its line and reopen it on the next
    lines until it closes, so each line reads as fully marked. A [header]
    always ends any open mark."""
    out = []
    open_i = open_b = False
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("["):
            open_i = open_b = False
            out.append(ln)
            continue
        if not s:
            out.append(ln)
            continue
        if open_b:
            s = "{" + s
        if open_i:
            s = "«" + s
        # state after this line: last mark char decides whether it is still open
        oi = s.rfind("«") > s.rfind("»")
        ob = s.rfind("{") > s.rfind("}")
        if oi:
            s = s + "»"
        if ob:
            s = s + "}"
        open_i, open_b = oi, ob
        out.append(s)
    return "\n".join(out)


def parse_parts(text, default_performer=""):
    """Parse marked lyrics.

    Returns dict:
      lines:      [{sec, text, performers: [names], words: [...]}]
      performers: ordered list of individual performer names
      counts:     {name: number of lines they sing}  (shared lines go to one singer, see below)
    """
    lines = []
    sec_label = ""
    cur = {"plain": [default_performer] if default_performer else [], "i": [], "b": []}
    last_by_kind = {}
    for raw in spread_marks(text).splitlines():
        ln = raw.strip()
        if not ln:
            continue
        hm = re.fullmatch(r"\[([^\]:]+)(?::\s*([^\]]*))?\]", ln)
        if hm:
            sec_label = ln
            kind = _section_kind(hm.group(1))
            spec = hm.group(2)
            if spec:
                names = _names(spec)
                marked_i = [n for n, m in names if m == "i"]
                marked_b = [n for n, m in names if m == "b"]
                plain = [n for n, m in names if m == ""]
                allnames = [n for n, _ in names]
                if marked_i or marked_b:
                    cur = {"plain": plain[:1] or allnames[:1],
                           "i": marked_i or (plain[1:2] if len(plain) > 1 else []),
                           "b": marked_b or allnames}
                else:
                    cur = {"plain": allnames, "i": allnames[1:2], "b": allnames}
                    cur["_multi_plain"] = len(allnames) > 1
                last_by_kind[kind] = cur
            else:
                cur = last_by_kind.get(kind) or {"plain": [default_performer] if default_performer else [],
                                                 "i": [], "b": []}
            continue
        mark = _line_mark(ln)
        if mark is None:
            continue
        words = clean_words(ln)
        if not words:
            continue
        # A multi-artist header with no marks anywhere in the section's lines
        # means "all of them" (resolved below); plain lines default to all.
        if mark == "i" and cur.get("i"):
            perf = cur["i"]
        elif mark == "b" and cur.get("b"):
            perf = cur["b"]
        else:
            perf = cur["plain"]
        # Marked ad-libs inside parentheses, e.g. "(«Swerve»)" -> Big Sean.
        # Only these can be told apart later (the engine looks for them in
        # the gaps); bare "{believe}" ad-libs meaning "everyone" are skipped.
        adlibs = []
        for paren in re.findall(r"\(([^)]*)\)", ln):
            if "«" in paren and cur.get("i"):
                adlibs += [p for p in cur["i"] if p not in adlibs]
            if "{" in paren and cur.get("b") and len(cur["b"]) == 1:
                adlibs += [p for p in cur["b"] if p not in adlibs]
        lines.append({"sec": sec_label, "text": ln, "performers": list(perf), "words": words,
                      "adlibs": [a for a in adlibs if a not in perf],
                      "_cur": id(cur), "_multi": cur.get("_multi_plain", False), "_mark": mark})

    # Sections whose header listed several unmarked artists: if any line in the
    # section is marked, plain lines go to the FIRST artist (Genius convention);
    # otherwise the whole section is sung together.
    by_sec = {}
    for ln in lines:
        by_sec.setdefault(ln["_cur"], []).append(ln)
    for sec_lines in by_sec.values():
        if not sec_lines[0]["_multi"]:
            continue
        if any(l["_mark"] for l in sec_lines):
            for l in sec_lines:
                if l["_mark"] == "":
                    l["performers"] = l["performers"][:1]
    # 2026-10-27: "even if genius says both people sing at the same time, it almost never sounds
    # good" -> a line credited to several singers goes to ONE: the lead if they're on it, else the first
    # listed. "credited" keeps everyone Genius named (backing_singer() scores hooks from it).
    for ln in lines:
        ln["credited"] = list(ln["performers"])
        if len(ln["performers"]) > 1:
            ln["performers"] = [default_performer if default_performer in ln["performers"]
                                else ln["performers"][0]]
    performers, counts = [], {}
    for ln in lines:
        for k in ("_cur", "_multi", "_mark"):
            ln.pop(k, None)
        if not ln["performers"]:
            ln["performers"] = ["(unknown)"]
        for p in ln["performers"]:
            if p not in counts:
                performers.append(p)
                counts[p] = 0
            counts[p] += 1
    return {"lines": lines, "performers": performers, "counts": counts}


def perf_key(performers):
    return " + ".join(performers)


if __name__ == "__main__":
    import sys
    page = open(sys.argv[1]).read()
    text, artist, title = parse_genius_html(page)
    print(artist, "|", title)
    p = parse_parts(text, artist)
    print(p["counts"])
    for ln in p["lines"]:
        print(f'{perf_key(ln["performers"])[:28]:28s} {ln["text"][:70]}')


# --- Who sings the backing vocals (2026-10-27) ---------------------------
# With "keep backing vocals" in a multi-voice song, the backing stem goes to the cast voice of whoever
# sings most of the hooks and ad-libs besides the lead (Lil Jon on "Yeah!", Nate Dogg on "Shake That").
# Solo songs are never touched: there the lead sings everything.
HOOK_SECTION = re.compile(r"chorus|hook|refrain|bridge|intro|outro", re.I)


def backing_singer(parts, lead, min_share=0.15, min_score=8):
    """(performer or None, {performer: score}). Score = words sung in hook-type sections (shared lines
    split evenly) + 4 per ad-lib line. The best non-lead performer wins if their score is at least
    min_share of all hook words; otherwise None (the lead does their own backing)."""
    score, hook_total = {}, 0.0
    for ln in parts["lines"]:
        perfs = ln.get("credited") or ln.get("performers") or []
        if HOOK_SECTION.search(ln.get("sec", "")) and perfs:
            n = len(ln.get("words", []))
            hook_total += n
            for p in perfs:
                score[p] = score.get(p, 0.0) + n / len(perfs)
        for a in ln.get("adlibs", []):
            score[a] = score.get(a, 0.0) + 4
    lead_k = perf_key(lead) if lead else ""
    others = {p: s for p, s in score.items() if perf_key(p) != lead_k}
    if not others:
        return None, score
    best = max(others, key=others.get)
    if others[best] < max(min_score, min_share * hook_total):
        return None, score
    return best, score
