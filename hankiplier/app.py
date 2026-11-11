#!/usr/bin/env python3
"""Hankiplier: multi-voice Hankify. One song, up to 10 singers, each part
sung by the character you cast.

  1. Drop a song in (or Browse).
  2. Paste lyrics, or give a genius.com URL and Fetch (Fetch keeps Genius's
     italic/bold marks, which say who sings which line in shared verses -
     pasted text loses them).
  3. Find parts -> cast a character (RVC voice) for each performer.
  4. Hankiply! -> renders each voice with the song pipeline, lines the lyrics
     up with the recording, picks the cuts, and mixes the result.
  5. Check the Sections table; double-click a row to fix a cut or who sings
     it, then Rebuild (seconds - nothing is re-rendered).

Runs with UVR5's venv python (tkinterdnd2), like Hankify, whose helpers it
reuses. The analysis/splicing engine runs in the RVC venv (engine.py).
"""
import datetime
import hashlib
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from tkinterdnd2 import TkinterDnD, DND_FILES

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lyrics as lyr  # noqa: E402
import hk_paths  # noqa: E402
import share  # noqa: E402

cfg = hk_paths.cfg
_spec = importlib.util.spec_from_file_location("hankify_app", cfg.HANKIFY_APP)
hk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hk)
hk.ERROR_LOG_PATH = os.path.join(HERE, "errors.log")  # hk.log_error writes here, not Hankify's log

RVC_PY = cfg.RVC_PYTHON
ENGINE = os.path.join(HERE, "engine.py")
PIPELINE_SCRIPT = hk.PIPELINE_SCRIPT
# The song pipeline runs under the same interpreter choice Hankify makes: on Windows the package has one venv for
# everything (this one); on Linux the pipeline is started with the system python3, which then picks each stage's venv.
PIPELINE_PY = sys.executable if os.name == "nt" else "python3"
INTERMEDIATES_DIR = cfg.INTERMEDIATES_DIR
PROJECTS_DIR = os.path.join(HERE, "projects")
CONFIG_PATH = os.path.join(HERE, "config.json")
HANKIFY_CONFIG = hk.CONFIG_PATH
MAX_PARTS = 10
ORIGINAL = "__original__"
ORIGINAL_LABEL = "Original vocal (untouched)"
STYLE = "Hankify"  # reuse Hankify's style names/palette


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def voice_label(model):
    return ORIGINAL_LABEL if model == ORIGINAL else model[:-4] if model.lower().endswith(".pth") else model


ORIGINAL_BACKING_LABEL = "Original backing"


def voice_key(voice, pitch, backing=False):
    if voice == ORIGINAL:
        return ORIGINAL
    return f"{voice}|{int(pitch)}" + ("|bv" if backing else "")


def slug(text):
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[^\w .,()&'!+-]+", "_", text).strip()[:80] or "song"


class SectionDialog(tk.Toplevel):
    """Edit one plan section: start time and who sings it."""

    def __init__(self, app, index):
        super().__init__(app)
        self.app, self.index = app, index
        sec = app.plan["sections"][index]
        p = app.palette
        self.title(f"Section {index + 1}")
        self.configure(bg=p["bg"])
        self.transient(app)
        frm = ttk.Frame(self, style=f"{STYLE}.TFrame", padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=sec.get("lyric", "")[:160], wraplength=420, style=f"{STYLE}.Dim.TLabel"
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frm, text="Starts at", style=f"{STYLE}.TLabel").grid(row=1, column=0, sticky="w")
        self.start_var = tk.StringVar(value=hk.format_time(sec["start"]))
        e = ttk.Entry(frm, textvariable=self.start_var, width=12, style=f"{STYLE}.TEntry")
        e.grid(row=1, column=1, sticky="w", pady=2)
        if index == 0:
            e.state(["disabled"])
        ttk.Label(frm, text="Sung by", style=f"{STYLE}.TLabel").grid(row=2, column=0, sticky="w")
        choices = list(app.parts["performers"])
        combos = {lyr.perf_key(s["performers"]) for s in app.plan["sections"] if len(s["performers"]) > 1}
        choices += sorted(combos)
        if len(app.parts["performers"]) > 1:
            everyone = lyr.perf_key(app.parts["performers"])
            if everyone not in choices:
                choices.append(everyone)
        self.who_var = tk.StringVar(value=lyr.perf_key(sec["performers"]))
        ttk.Combobox(frm, textvariable=self.who_var, values=choices, state="readonly", width=40,
                     style=f"{STYLE}.TCombobox").grid(row=2, column=1, sticky="w", pady=2)
        btns = ttk.Frame(frm, style=f"{STYLE}.TFrame")
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        if index > 0:
            ttk.Button(btns, text="Delete section", style=f"{STYLE}.TButton",
                       command=self._delete).pack(side="left", padx=3)
        ttk.Button(btns, text="Split here…", style=f"{STYLE}.TButton",
                   command=self._split).pack(side="left", padx=3)
        ttk.Button(btns, text="Save", style=f"{STYLE}.Accent.TButton",
                   command=self._save).pack(side="left", padx=3)
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())

    def _parse(self):
        t = hk.parse_time(self.start_var.get()) if self.index > 0 else 0.0
        if t is None:
            messagebox.showerror("Hankiplier", "Start time must look like 1:42.5", parent=self)
        return t

    def _save(self):
        t = self._parse()
        if t is None:
            return
        secs = self.app.plan["sections"]
        secs[self.index]["start"] = round(t, 2)
        secs[self.index]["performers"] = self.who_var.get().split(" + ")
        secs[self.index]["edited"] = True
        secs.sort(key=lambda s: s["start"])
        self.app.plan_changed()
        self.destroy()

    def _split(self):
        """Add a new section after this one's start (defaults to +2 s)."""
        secs = self.app.plan["sections"]
        s = secs[self.index]
        nxt = secs[self.index + 1]["start"] if self.index + 1 < len(secs) else self.app.plan["duration"]
        t = min(s["start"] + 2.0, (s["start"] + nxt) / 2)
        secs.insert(self.index + 1, {"start": round(t, 2), "performers": list(s["performers"]),
                                     "lyric": "(split)", "edited": True})
        self.app.plan_changed()
        self.destroy()
        self.app.after(50, lambda: SectionDialog(self.app, self.index + 1))

    def _delete(self):
        del self.app.plan["sections"][self.index]
        self.app.plan_changed()
        self.destroy()


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hankiplier")
        self.geometry("1320x840")
        self.minsize(1000, 640)
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        self.cfg = load_json(CONFIG_PATH, {})
        hcfg = load_json(HANKIFY_CONFIG, {})
        theme = self.cfg.get("theme") or hcfg.get("theme") or hk.DEFAULT_THEME
        self.theme_name = theme if theme in hk.THEMES else hk.DEFAULT_THEME
        self.palette = hk.THEMES[self.theme_name]
        try:
            self.index_rate = float(self.cfg.get("index_rate", hcfg.get("index_rate", hk.DEFAULT_INDEX_RATE)))
        except (TypeError, ValueError):
            self.index_rate = hk.DEFAULT_INDEX_RATE
        self.output_dir = self.cfg.get("output_dir") or hk.OUTPUT_DIR

        self.ui_queue = queue.Queue()
        self.worker = None
        self.procs = []
        self.song = ""
        self.pdir = None
        self.proj = {}
        self.parts = None
        self.plan = None
        self.cast_rows = []
        self.voice_models = hk.list_voice_models()
        self.voice_choices = [ORIGINAL_LABEL] + [voice_label(v) for v in self.voice_models]
        self._label_to_model = {voice_label(v): v for v in self.voice_models}
        self._label_to_model[ORIGINAL_LABEL] = ORIGINAL

        self.song_var = tk.StringVar(value="Drop a song here, or Browse…")
        self.url_var = tk.StringVar()
        self.default_singer_var = tk.StringVar()
        self.outname_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Step 1: drop a song in.")
        self.theme_var = tk.StringVar(value=self.theme_name)

        self._build_menu()
        self._build_style()
        self._build_ui()
        self._apply_theme()
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self.on_drop)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._process_ui_queue)

    # ------------------------------------------------------------------ look
    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Import lyrics and cuts from old project…", command=self.import_old_project)
        file_menu.add_separator()
        file_menu.add_command(label="Export setup to share (.zip)…", command=self.export_setup)
        file_menu.add_command(label="Import shared setup…", command=self.import_setup)
        menubar.add_cascade(label="File", menu=file_menu)
        theme_menu = tk.Menu(menubar, tearoff=0)
        for key, label in hk.THEME_LABELS:
            theme_menu.add_radiobutton(label=label, variable=self.theme_var, value=key,
                                       command=lambda k=key: self.set_theme(k))
        menubar.add_cascade(label="Theme", menu=theme_menu)
        self.config(menu=menubar)

    def _build_style(self):
        hk.App._build_style(self)  # same palette-driven styles as Hankify
        p = self.palette
        style = ttk.Style(self)
        style.configure(f"{STYLE}.Treeview", background=p["bg_alt"], fieldbackground=p["bg_alt"],
                        foreground=p["fg"], bordercolor=p["border"], rowheight=22)
        style.map(f"{STYLE}.Treeview", background=[("selected", p["accent"])],
                  foreground=[("selected", p["accent_fg"])])
        style.configure(f"{STYLE}.Treeview.Heading", background=p["bg"], foreground=p["fg"],
                        bordercolor=p["border"])
        style.configure(f"{STYLE}.TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure(f"{STYLE}.TLabelframe.Label", background=p["bg"], foreground=p["fg"],
                        font=("", 11, "bold"))
        style.configure(f"{STYLE}.TSpinbox", fieldbackground=p["bg_alt"], foreground=p["fg"],
                        background=p["bg_alt"], arrowcolor=p["fg"], bordercolor=p["border"])

    def set_theme(self, name):
        self.theme_name = name
        self.palette = hk.THEMES[name]
        self.cfg["theme"] = name
        self._save_cfg()
        self._build_style()
        self._apply_theme()

    def _apply_theme(self):
        p = self.palette
        self.configure(bg=p["bg"])
        for t in (self.lyrics_text, self.log_text):
            t.configure(bg=p["bg_alt"], fg=p["fg"], insertbackground=p["fg"],
                        selectbackground=p["accent"], selectforeground=p["accent_fg"],
                        highlightbackground=p["border"], highlightcolor=p["accent"])
        self.drop_label.configure(bg=p["bg_alt"], fg=p["fg"], highlightbackground=p["border"])
        self.lyrics_text.tag_configure("header", foreground=p["accent"], font=("", 10, "bold"))
        self.lyrics_text.tag_configure("ital", foreground=p["fg_dim"], font=("", 10, "italic"))
        self.lyrics_text.tag_configure("bold", font=("", 10, "bold"))

    # ------------------------------------------------------------------ layout
    def _frame(self, parent, text):
        return ttk.LabelFrame(parent, text=text, style=f"{STYLE}.TLabelframe", padding=8)

    def _build_ui(self):
        root = ttk.Frame(self, style=f"{STYLE}.TFrame", padding=10)
        root.pack(fill="both", expand=True)
        head = ttk.Frame(root, style=f"{STYLE}.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Hankiplier", font=("", 18, "bold"), style=f"{STYLE}.TLabel").pack(side="left")
        ttk.Label(head, text="  one song, many characters", style=f"{STYLE}.Dim.TLabel").pack(side="left", pady=(6, 0))

        body = ttk.Frame(root, style=f"{STYLE}.TFrame")
        body.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(body, style=f"{STYLE}.TFrame")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(body, style=f"{STYLE}.TFrame")
        right.pack(side="left", fill="both", expand=True)

        # --- song
        sf = self._frame(left, "1. Song")
        sf.pack(fill="x")
        self.drop_label = tk.Label(sf, textvariable=self.song_var, anchor="w", padx=8, pady=8,
                                   relief="groove", highlightthickness=1)
        self.drop_label.pack(side="left", fill="x", expand=True)
        ttk.Button(sf, text="Browse…", style=f"{STYLE}.TButton", command=self.browse_song
                   ).pack(side="left", padx=(6, 0))

        # --- lyrics
        lf = self._frame(left, "2. Lyrics")
        lf.pack(fill="both", expand=True, pady=(8, 0))
        urow = ttk.Frame(lf, style=f"{STYLE}.TFrame")
        urow.pack(fill="x")
        ttk.Label(urow, text="genius.com URL", style=f"{STYLE}.TLabel").pack(side="left")
        ue = ttk.Entry(urow, textvariable=self.url_var, style=f"{STYLE}.TEntry")
        ue.pack(side="left", fill="x", expand=True, padx=6)
        ue.bind("<Return>", lambda e: self.fetch_lyrics())
        self.fetch_btn = ttk.Button(urow, text="Fetch", style=f"{STYLE}.TButton", command=self.fetch_lyrics)
        self.fetch_btn.pack(side="left")
        ttk.Label(lf, style=f"{STYLE}.Dim.TLabel",
                  text="…or paste lyrics below. [Verse 1: A & «B»] headers name the singers; "
                       "«italic» lines = second singer, {bold} = everyone. Fetch keeps Genius's marks; paste loses them."
                  , wraplength=500, justify="left").pack(fill="x", pady=(4, 4))
        tf = ttk.Frame(lf, style=f"{STYLE}.TFrame")
        tf.pack(fill="both", expand=True)
        self.lyrics_text = tk.Text(tf, wrap="word", undo=True, font=("", 10), relief="flat",
                                   highlightthickness=1, padx=6, pady=4)
        sb = ttk.Scrollbar(tf, command=self.lyrics_text.yview)
        self.lyrics_text.configure(yscrollcommand=sb.set)
        self.lyrics_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.lyrics_text.bind("<<Modified>>", self._on_lyrics_modified)
        brow = ttk.Frame(lf, style=f"{STYLE}.TFrame")
        brow.pack(fill="x", pady=(6, 0))
        ttk.Label(brow, text="Singer for unlabelled sections", style=f"{STYLE}.TLabel").pack(side="left")
        ttk.Entry(brow, textvariable=self.default_singer_var, width=22, style=f"{STYLE}.TEntry"
                  ).pack(side="left", padx=6)
        ttk.Button(brow, text="Find parts →", style=f"{STYLE}.Accent.TButton",
                   command=self.find_parts).pack(side="right")

        # --- cast
        cf = self._frame(right, "3. Cast (up to 10 parts)")
        cf.pack(fill="x")
        self.cast_frame = ttk.Frame(cf, style=f"{STYLE}.TFrame")
        self.cast_frame.pack(fill="x")
        self.cast_hint = ttk.Label(self.cast_frame, text="Find parts to list the performers here.",
                                   style=f"{STYLE}.Dim.TLabel")
        self.cast_hint.grid(row=0, column=0, sticky="w")
        orow = ttk.Frame(cf, style=f"{STYLE}.TFrame")
        orow.pack(fill="x", pady=(8, 0))
        ttk.Label(orow, text="Output name", style=f"{STYLE}.TLabel").pack(side="left")
        ttk.Entry(orow, textvariable=self.outname_var, style=f"{STYLE}.TEntry").pack(
            side="left", fill="x", expand=True, padx=6)
        self.go_btn = ttk.Button(orow, text="Hankiply!", style=f"{STYLE}.Accent.TButton", command=self.hankiply)
        self.go_btn.pack(side="left")

        # --- sections
        pf = self._frame(right, "4. Sections (double-click to fix a cut)")
        pf.pack(fill="both", expand=True, pady=(8, 0))
        tv = ttk.Frame(pf, style=f"{STYLE}.TFrame")
        tv.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tv, columns=("start", "who", "voice", "lyric"), show="headings",
                                 style=f"{STYLE}.Treeview", selectmode="browse")
        for col, w, txt in (("start", 70, "Start"), ("who", 150, "Performer"), ("voice", 160, "Voice"),
                            ("lyric", 260, "Lyric")):
            self.tree.heading(col, text=txt, anchor="w")
            self.tree.column(col, width=w, anchor="w", stretch=(col == "lyric"))
        tsb = ttk.Scrollbar(tv, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self.on_section_double_click)
        prow = ttk.Frame(pf, style=f"{STYLE}.TFrame")
        prow.pack(fill="x", pady=(6, 0))
        self.rebuild_btn = ttk.Button(prow, text="Rebuild song", style=f"{STYLE}.TButton", command=self.rebuild)
        self.rebuild_btn.pack(side="left")
        self.reanalyze_btn = ttk.Button(prow, text="Re-detect cuts", style=f"{STYLE}.TButton",
                                        command=self.reanalyze)
        self.reanalyze_btn.pack(side="left", padx=6)
        self.play_btn = ttk.Button(prow, text="Play result", style=f"{STYLE}.TButton", command=self.play_output)
        self.play_btn.pack(side="right")
        ttk.Button(prow, text="Open folder", style=f"{STYLE}.TButton", command=self.open_folder
                   ).pack(side="right", padx=6)

        # --- log
        gf = self._frame(right, "Progress")
        gf.pack(fill="x", pady=(8, 0))
        ttk.Label(gf, textvariable=self.status_var, style=f"{STYLE}.TLabel").pack(fill="x")
        self.progress = ttk.Progressbar(gf, mode="indeterminate", style=f"{STYLE}.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(4, 4))
        self.log_text = tk.Text(gf, height=7, wrap="word", font=("monospace", 9), relief="flat",
                                highlightthickness=1, state="disabled")
        self.log_text.pack(fill="x")
        self._update_buttons()

    # ------------------------------------------------------------------ threading helpers
    def _process_ui_queue(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(50, self._process_ui_queue)

    def ui(self, fn):
        self.ui_queue.put(fn)

    def log(self, msg):
        def add():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg.rstrip() + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.ui(add)

    def status(self, msg):
        self.ui(lambda: self.status_var.set(msg))

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _update_buttons(self):
        busy = self.busy()
        for b in (self.go_btn, self.fetch_btn):
            b.state(["disabled"] if busy else ["!disabled"])
        has_plan = bool(self.plan) and not busy
        for b in (self.rebuild_btn, self.reanalyze_btn):
            b.state(["!disabled"] if has_plan else ["disabled"])
        out = self.proj.get("last_output")
        self.play_btn.state(["!disabled"] if out and os.path.exists(out) else ["disabled"])

    def run_worker(self, fn):
        if self.busy():
            return
        self.progress.start(12)

        def wrap():
            try:
                fn()
            except Exception as e:  # show every failure, keep the app alive
                self.log(f"ERROR: {e}")
                self.status(f"Failed: {e}")
                hk.log_error(self.song, repr(e))
            finally:
                self.ui(self.progress.stop)
                self.ui(self._update_buttons)
        self.worker = threading.Thread(target=wrap, daemon=True)
        self.worker.start()
        self._update_buttons()

    def run_cmd(self, cmd, label, env=None):
        """Run a subprocess, streaming its output into the log."""
        self.log(f"$ {label}")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             preexec_fn=hk._die_with_parent, bufsize=1,
                             env={**os.environ, **env} if env else None)
        self.procs.append(p)
        tail = []
        for line in p.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or "warnings.warn" in line:
                continue
            tail = (tail + [line])[-15:]
            self.log("  " + line)
        p.wait()
        self.procs.remove(p)
        if p.returncode != 0:
            raise RuntimeError(f"{label} failed:\n" + "\n".join(tail))

    # ------------------------------------------------------------------ project
    def _save_cfg(self):
        save_json(CONFIG_PATH, self.cfg)

    def _project_dir_for(self, song):
        h = hashlib.sha1(os.path.abspath(song).encode()).hexdigest()[:8]
        base = os.path.splitext(os.path.basename(song))[0]
        return os.path.join(PROJECTS_DIR, f"{slug(base)} [{h}]")

    def save_project(self):
        if not self.pdir:
            return
        os.makedirs(self.pdir, exist_ok=True)
        self.proj["lyrics_text"] = self.lyrics_text.get("1.0", "end-1c")
        self.proj["genius_url"] = self.url_var.get().strip()
        self.proj["default_singer"] = self.default_singer_var.get().strip()
        self.proj["out_name"] = self.outname_var.get().strip()
        self.proj["casting"] = self.current_casting(partial=True)
        if getattr(self, "backing_voice_var", None):
            self.proj["backing_voice"] = self.current_backing_voice()
        save_json(os.path.join(self.pdir, "project.json"), self.proj)
        if self.plan:
            save_json(os.path.join(self.pdir, "plan.json"), self.plan)

    def set_song(self, path):
        if self.busy():
            return
        if not os.path.isfile(path) or not path.lower().endswith(hk.AUDIO_EXTENSIONS):
            messagebox.showerror("Hankiplier", f"Not an audio file:\n{path}")
            return
        if self.pdir:
            self.save_project()
        self.song = path
        self.song_var.set(path)
        self.pdir = self._project_dir_for(path)
        self.proj = load_json(os.path.join(self.pdir, "project.json"), {})
        self.proj["song"] = path
        self.proj.setdefault("name", os.path.splitext(os.path.basename(path))[0])
        self.proj.setdefault("renders", {})
        self.cfg["last_dir"] = os.path.dirname(path)
        self._save_cfg()
        # restore a previous session on this song
        self._set_lyrics(self.proj.get("lyrics_text", ""))
        self.url_var.set(self.proj.get("genius_url", ""))
        self.default_singer_var.set(self.proj.get("default_singer", ""))
        self.outname_var.set(self.proj.get("out_name") or f'{self.proj["name"]} (Hankiplier)')
        self.parts = self.proj.get("parts")
        self.plan = load_json(os.path.join(self.pdir, "plan.json"), None)
        if self.plan and self.plan.get("parts_hash") != self.proj.get("parts_hash"):
            self.plan = None
        self._build_cast_rows()
        self._refresh_tree()
        self.status_var.set("Step 2: paste lyrics or fetch them from Genius." if not self.parts
                            else "Restored this song's last session.")
        self._update_buttons()

    def _list_projects(self):
        rows = []
        for d in os.listdir(PROJECTS_DIR):
            pj = os.path.join(PROJECTS_DIR, d, "project.json")
            if os.path.isfile(pj):
                rows.append((os.path.getmtime(pj), os.path.join(PROJECTS_DIR, d)))
        return sorted(rows, reverse=True)

    def import_old_project(self):
        """Copy ONLY the lyrics and the cuts (plan) of an old project onto the current song.
        Casting, renders and outputs are not carried over."""
        if self.busy():
            return
        if not self.song:
            messagebox.showinfo("Hankiplier", "Drop the song in first, then import the old lyrics & cuts.")
            return
        win = tk.Toplevel(self); win.title("Import lyrics & cuts from old project"); win.geometry("760x540"); win.transient(self)
        q = tk.StringVar()
        tk.Entry(win, textvariable=q).pack(fill="x", padx=8, pady=(8, 4))
        lb = tk.Listbox(win, activestyle="dotbox"); lb.pack(fill="both", expand=True, padx=8, pady=4)
        rows = self._list_projects(); shown = []
        def refill(*_):
            lb.delete(0, "end"); shown.clear()
            terms = q.get().lower().split()
            for m, d in rows:
                if all(t in os.path.basename(d).lower() for t in terms):
                    shown.append(d)
                    lb.insert("end", f"{time.strftime('%Y-%m-%d', time.localtime(m))}   {os.path.basename(d)}")
        def apply(src):
            win.destroy()
            self._apply_old_project(src)
        def go(*_):
            if lb.curselection():
                apply(shown[lb.curselection()[0]])
        def browse():
            d = hk.pick_dir("Choose a project folder (has project.json)", PROJECTS_DIR)
            if d:
                apply(d)
        q.trace_add("write", refill); lb.bind("<Double-Button-1>", go); lb.bind("<Return>", go)
        bar = tk.Frame(win); bar.pack(pady=(0, 8))
        tk.Button(bar, text="Import", command=go).pack(side="left", padx=4)
        tk.Button(bar, text="Browse for folder…", command=browse).pack(side="left", padx=4)
        refill()

    def _apply_old_project(self, src):
        old = load_json(os.path.join(src, "project.json"), None)
        if not old or not old.get("parts"):
            messagebox.showerror("Hankiplier", f"No lyrics found in a project.json in:\n{src}")
            return
        plan = load_json(os.path.join(src, "plan.json"), None)
        if plan and plan.get("parts_hash") != old.get("parts_hash"):
            plan = None
        if self.plan and not messagebox.askyesno("Hankiplier", "Replace this song's current lyrics and cuts?"):
            return
        self.proj["parts"] = self.parts = old["parts"]
        self.proj["parts_hash"] = old.get("parts_hash")
        for k in ("lyrics_text", "genius_url", "default_singer"):
            self.proj[k] = old.get(k, "")
        self._set_lyrics(self.proj["lyrics_text"])
        self.url_var.set(self.proj["genius_url"])
        self.default_singer_var.set(self.proj["default_singer"])
        self.plan = plan
        self.proj["casting"] = {}
        self._build_cast_rows()
        self._refresh_tree()
        self.save_project()
        self.status_var.set("Imported lyrics" + (" and cuts" if plan else " (that project had no cuts)")
                            + f" from {os.path.basename(src)}. Pick a character for each part.")
        self._update_buttons()

    def export_setup(self):
        """Bundle this song's lyrics, parts, cuts and casting into a zip named after the song (no audio)."""
        if not self.song or not self.proj.get("parts"):
            messagebox.showinfo("Hankiplier", "Open a song and get its lyrics/parts first.")
            return
        self.save_project()
        try:
            sh = share.build_share(self.proj, self.plan, self.song)
        except share.ShareError as e:
            messagebox.showerror("Hankiplier", str(e))
            return
        out = filedialog.asksaveasfilename(title="Export setup", defaultextension=".zip",
                                           initialdir=self.output_dir, initialfile=share.default_bundle_name(sh),
                                           filetypes=[("Hankiplier setup", "*.zip")])
        if out:
            share.write_bundle(out, sh)
            self.status_var.set(f"Exported setup: {os.path.basename(out)}"
                                + ("" if sh["cuts"] else " (no cuts yet - only lyrics and parts)"))

    def import_setup(self):
        """Apply a shared setup (zip or setup.json) to the song that is open now."""
        if self.busy():
            return
        if not self.song:
            messagebox.showinfo("Hankiplier", "Drop your copy of the song in first, then import the shared setup.")
            return
        path = filedialog.askopenfilename(title="Import shared setup", initialdir=self.cfg.get("last_dir"),
                                          filetypes=[("Hankiplier setup", "*.zip *.json"), ("All files", "*")])
        if not path:
            return
        try:
            sh = share.read_share(path)
        except share.ShareError as e:
            messagebox.showerror("Hankiplier", str(e))
            return
        level, msg = share.check_song(sh, self.song)
        if level == "mismatch" and not messagebox.askyesno("Hankiplier", msg + "\n\nImport anyway?"):
            return
        if self.plan and not messagebox.askyesno("Hankiplier", "Replace this song's current lyrics, cuts and casting?"):
            return
        plan, missing = share.apply_to_proj(self.proj, sh, self.voice_models)
        self.parts = self.proj["parts"]
        self._set_lyrics(self.proj["lyrics_text"])
        self.url_var.set(self.proj["genius_url"])
        self.default_singer_var.set(self.proj["default_singer"])
        self.plan = plan
        self._build_cast_rows()
        self._refresh_tree()
        self.save_project()
        note = f"Imported {os.path.basename(path)} ({msg})"
        if missing:
            note += " Not installed, left uncast: " + ", ".join(f"{w} ({share.voice_label(v)})" for w, v in missing)
        self.status_var.set(note)
        self._update_buttons()

    def browse_song(self):
        path = hk.pick_file("Choose a song", [("Audio", " ".join(f"*{e}" for e in hk.AUDIO_EXTENSIONS))],
                            self.cfg.get("last_dir"))
        if path:
            self.set_song(path)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        if paths:
            self.set_song(paths[0])

    # ------------------------------------------------------------------ lyrics
    def _set_lyrics(self, text):
        self.lyrics_text.delete("1.0", "end")
        self.lyrics_text.insert("1.0", text)
        self._highlight_lyrics()
        self.lyrics_text.edit_modified(False)

    def _on_lyrics_modified(self, event=None):
        if self.lyrics_text.edit_modified():
            self.lyrics_text.edit_modified(False)
            self.after_idle(self._highlight_lyrics)

    def _highlight_lyrics(self):
        t = self.lyrics_text
        for tag in ("header", "ital", "bold"):
            t.tag_remove(tag, "1.0", "end")
        text = t.get("1.0", "end-1c")
        for tag, pat in (("header", r"^\[[^\]]*\]"), ("ital", r"«[^«»]*»"), ("bold", r"\{[^{}]*\}")):
            for m in re.finditer(pat, text, re.M):
                t.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

    def fetch_lyrics(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if "genius.com" not in url:
            messagebox.showerror("Hankiplier", "That doesn't look like a genius.com lyrics URL.")
            return

        def work():
            self.status("Fetching lyrics from Genius…")
            text, artist, title = lyr.fetch_genius(url)
            if not text.strip():
                raise RuntimeError("No lyrics found on that page - paste them instead.")

            def done():
                self._set_lyrics(text)
                if artist and not self.default_singer_var.get():
                    self.default_singer_var.set(artist.split(",")[0].split(" & ")[0].strip())
                if artist and title:
                    self.proj["title"] = title
                    self.proj["artist"] = artist
                    if not self.proj.get("out_name"):
                        self.outname_var.set(f"{artist} - {title} (Hankiplier)")
                self.status_var.set("Lyrics fetched. Step 3: Find parts.")
                self.find_parts()
            self.ui(done)
        self.run_worker(work)

    def find_parts(self):
        if not self.song:
            messagebox.showinfo("Hankiplier", "Drop a song in first.")
            return
        text = self.lyrics_text.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("Hankiplier", "Paste lyrics or fetch them from Genius first.")
            return
        parts = lyr.parse_parts(text, self.default_singer_var.get().strip())
        n = len(parts["performers"])
        if n == 0:
            messagebox.showerror("Hankiplier", "Couldn't find any lyric lines.")
            return
        if n > MAX_PARTS:
            messagebox.showerror(
                "Hankiplier",
                f"Found {n} performers - Hankiplier handles up to {MAX_PARTS}.\n\n"
                + ", ".join(parts["performers"])
                + "\n\nMerge some by editing the [Section: …] headers.")
            return
        if n == 1 and not any(re.match(r"\[[^\]]*:", l) for l in text.splitlines()):
            messagebox.showinfo(
                "Hankiplier",
                "No [Section: Singer] headers found, so every line went to one singer.\n"
                "Add headers like [Verse 1: Big Sean] to split the song.")
        h = hashlib.sha1(json.dumps(parts["lines"], sort_keys=True).encode()).hexdigest()
        self.parts = parts
        self.proj["parts"] = parts
        if h != self.proj.get("parts_hash"):
            self.proj["parts_hash"] = h
            self.plan = None
            if self.pdir and os.path.exists(os.path.join(self.pdir, "plan.json")):
                os.remove(os.path.join(self.pdir, "plan.json"))
        self._build_cast_rows()
        self._refresh_tree()
        self.save_project()
        self.status_var.set(f"Found {n} part{'s' if n != 1 else ''}. Step 3: pick a character for each, then Hankiply!")
        self._update_buttons()

    # ------------------------------------------------------------------ casting
    def _build_cast_rows(self):
        for w in self.cast_frame.winfo_children():
            w.destroy()
        self.cast_rows = []
        self.backing_voice_var = None
        if not self.parts:
            ttk.Label(self.cast_frame, text="Find parts to list the performers here.",
                      style=f"{STYLE}.Dim.TLabel").grid(row=0, column=0, sticky="w")
            return
        saved = self.proj.get("casting", {})
        memory = self.cfg.get("casting_memory", {})
        for col, txt in enumerate(("Performer", "Lines", "Character", "Pitch", "Keep backing")):
            ttk.Label(self.cast_frame, text=txt, style=f"{STYLE}.Dim.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 8))
        for i, perf in enumerate(self.parts["performers"]):
            c = saved.get(perf) or memory.get(perf) or {}
            model = c.get("voice")
            label = voice_label(model) if model and (model == ORIGINAL or model in self.voice_models) else ""
            voice_var = tk.StringVar(value=label)
            pitch_var = tk.StringVar(value=str(c.get("pitch", 0)))
            ttk.Label(self.cast_frame, text=perf, style=f"{STYLE}.TLabel", font=("", 10, "bold")).grid(
                row=i + 1, column=0, sticky="w", padx=(0, 8), pady=1)
            ttk.Label(self.cast_frame, text=str(self.parts["counts"][perf]), style=f"{STYLE}.Dim.TLabel").grid(
                row=i + 1, column=1, sticky="w", padx=(0, 8))
            cb = ttk.Combobox(self.cast_frame, textvariable=voice_var, values=self.voice_choices,
                              state="readonly", width=34, style=f"{STYLE}.TCombobox", height=25)
            cb.grid(row=i + 1, column=2, sticky="w", padx=(0, 8), pady=1)
            cb.bind("<<ComboboxSelected>>", lambda e: (self.save_project(), self._refresh_tree()))
            sp = ttk.Spinbox(self.cast_frame, from_=-24, to=24, textvariable=pitch_var, width=4,
                             style=f"{STYLE}.TSpinbox", command=self.save_project)
            sp.grid(row=i + 1, column=3, sticky="w")
            bv_var = tk.BooleanVar(value=bool(c.get("backing")))
            chk = ttk.Checkbutton(self.cast_frame, variable=bv_var, style=f"{STYLE}.TCheckbutton",
                                  command=self.save_project)
            chk.grid(row=i + 1, column=4, padx=(14, 0))
            hk.Tooltip(chk, lambda: ("Convert only this singer's lead vocal and keep the original\n"
                                     "backing vocals under it (for stacked/harmonised parts).\n"
                                     "First use per song splits the vocals on the CPU (~8 min).",
                                     self.palette["bg_alt"], self.palette["fg"]))
            self.cast_rows.append((perf, voice_var, pitch_var, bv_var))
        bv_model = self.proj.get("backing_voice")
        # Not chosen yet: whoever the lyrics say sings most of the hooks/ad-libs besides the lead, else the lead
        lead = self.default_singer_var.get().strip()
        who = lyr.backing_singer(self.parts, lead)[0] or lead
        if "backing_voice" not in self.proj:
            vv = next((vv for perf, vv, _, _ in self.cast_rows if perf == who), None)
            bv_model = self._label_to_model.get(vv.get()) if vv else None
        self.backing_voice_var = tk.StringVar(
            value=voice_label(bv_model) if bv_model in self.voice_models else ORIGINAL_BACKING_LABEL)
        row = len(self.parts["performers"]) + 1
        ttk.Label(self.cast_frame, text="Backing vocals sung by", style=f"{STYLE}.Dim.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=(0, 8), pady=(6, 1))
        bcb = ttk.Combobox(self.cast_frame, textvariable=self.backing_voice_var,
                           values=[ORIGINAL_BACKING_LABEL] + self.voice_choices[1:],
                           state="readonly", width=34, style=f"{STYLE}.TCombobox", height=25)
        bcb.grid(row=row, column=2, sticky="w", padx=(0, 8), pady=(6, 1))
        bcb.bind("<<ComboboxSelected>>", lambda e: self.save_project())
        if who:
            ttk.Label(self.cast_frame, text=f"lyrics: {who}", style=f"{STYLE}.Dim.TLabel").grid(
                row=row, column=3, columnspan=2, sticky="w", pady=(6, 1))
        hk.Tooltip(bcb, lambda: ("Who sings the backing vocals in the \"Keep backing\" parts.\n"
                                 "A character here converts the split-off backing stem with that voice\n"
                                 "(any RVC voice); \"Original backing\" keeps the real backing singers.",
                                 self.palette["bg_alt"], self.palette["fg"]))

    def current_backing_voice(self):
        var = getattr(self, "backing_voice_var", None)
        return self._label_to_model.get(var.get()) if var else None

    def current_casting(self, partial=False):
        cast = {}
        for perf, vv, pv, bv in self.cast_rows:
            model = self._label_to_model.get(vv.get())
            if not model:
                if partial:
                    continue
                return None
            try:
                pitch = int(pv.get())
            except ValueError:
                pitch = 0
            cast[perf] = {"voice": model, "pitch": pitch, "backing": bool(bv.get()) and model != ORIGINAL}
        return cast

    # ------------------------------------------------------------------ sections table
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        if not self.plan:
            return
        cast = self.current_casting(partial=True)
        for i, s in enumerate(self.plan["sections"]):
            who = lyr.perf_key(s["performers"])
            voice = " + ".join(voice_label(cast[p]["voice"]) if p in cast else "?" for p in s["performers"])
            flag = "" if s.get("confident", True) or s.get("edited") else "  (check)"
            self.tree.insert("", "end", iid=str(i), values=(
                hk.format_time(s["start"]), who, voice, (s.get("lyric") or "") + flag))

    def on_section_double_click(self, event):
        if self.busy() or not self.plan:
            return
        sel = self.tree.focus()
        if sel:
            SectionDialog(self, int(sel))

    def plan_changed(self):
        secs = self.plan["sections"]
        secs.sort(key=lambda s: s["start"])
        secs[0]["start"] = 0.0
        self.save_project()
        self._refresh_tree()
        self.status_var.set("Sections edited - Rebuild song to hear it.")

    # ------------------------------------------------------------------ the work
    def _render_name(self, voice, pitch, backing=False):
        stem = voice_label(voice)
        return (f'{slug(self.proj["name"])} [{slug(stem)}{f" {pitch:+d}" if pitch else ""}'
                f'{" lead only" if backing else ""}]')

    def _render_paths(self, voice, pitch, backing=False):
        name = self._render_name(voice, pitch, backing)
        d = os.path.join(INTERMEDIATES_DIR, name)
        return name, d, os.path.join(d, f"{name}_(Converted Vocals).wav")

    def _adopt_render(self, name, d, conv):
        """The pipeline keeps a failed run's folder and saves the next try as
        '<name> (1)', '(2)'... Find the newest complete one and move it to the
        canonical name. Returns True if conv now exists."""
        if os.path.exists(conv):
            return True
        pat = re.compile(re.escape(name) + r"( \(\d+\))?$")
        cands = []
        try:
            entries = os.listdir(INTERMEDIATES_DIR)
        except OSError:
            return False
        for e in entries:
            if pat.fullmatch(e):
                f = os.path.join(INTERMEDIATES_DIR, e, f"{name}_(Converted Vocals).wav")
                if os.path.exists(f):
                    cands.append((os.path.getmtime(f), e))
        if not cands:
            return False
        best = os.path.join(INTERMEDIATES_DIR, max(cands)[1])
        if best != d:
            self._clear_incomplete(d, name)
            if os.path.exists(d):  # something unexpected is there - use the copy in place
                return False
            os.rename(best, d)
            self.log(f"  (moved {os.path.basename(best)} -> {name})")
        return os.path.exists(conv)

    def _clear_incomplete(self, d, name):
        """Remove a render folder left by a failed run (stems + source.json
        only, no converted vocal), so the pipeline reuses the name."""
        if not os.path.isdir(d) or os.path.exists(os.path.join(d, f"{name}_(Converted Vocals).wav")):
            return
        allowed = re.compile(r"([12]_.*_\((Vocals|Instrumental|Vocals_lead_only|Vocals_backing_only)\)\.wav"
                             r"|source\.json)$")
        files = os.listdir(d)
        if all(allowed.match(f) for f in files):
            for f in files:
                os.remove(os.path.join(d, f))
            os.rmdir(d)

    def _find_backing_stems(self, d):
        try:
            files = os.listdir(d)
        except OSError:
            return None
        lead = next((f for f in files if f.endswith("(Vocals_lead_only).wav")), None)
        back = next((f for f in files if f.endswith("(Vocals_backing_only).wav")), None)
        return {"lead": os.path.join(d, lead), "backing": os.path.join(d, back)} if lead and back else None

    def _find_stems(self, d):
        try:
            files = os.listdir(d)
        except OSError:
            return None
        voc = next((f for f in files if f.endswith("_(Vocals).wav")), None)
        ins = next((f for f in files if f.endswith("_(Instrumental).wav")), None)
        return {"vocals": os.path.join(d, voc), "instrumental": os.path.join(d, ins)} if voc and ins else None

    def _unique_output(self, name):
        base = os.path.join(self.output_dir, slug(name))
        path = base + ".mp3"
        v = 2
        while os.path.exists(path):
            path = f"{base} v{v}.mp3"
            v += 1
        return path

    def hankiply(self):
        if not self.song:
            messagebox.showinfo("Hankiplier", "Drop a song in first.")
            return
        if not self.parts:
            self.find_parts()
            if not self.parts:
                return
        cast = self.current_casting()
        if cast is None:
            missing = [p for p, vv, *_ in self.cast_rows if not self._label_to_model.get(vv.get())]
            messagebox.showinfo("Hankiplier", "Pick a character for: " + ", ".join(missing))
            return
        if all(c["voice"] == ORIGINAL for c in cast.values()):
            messagebox.showinfo("Hankiplier", "Everyone is set to the original vocal - nothing to do.")
            return
        self.proj["casting"] = cast
        mem = self.cfg.setdefault("casting_memory", {})
        mem.update(cast)
        self._save_cfg()
        self.save_project()
        self.run_worker(lambda: self._do_hankiply(cast))

    def _do_hankiply(self, cast):
        voices = []
        for perf in self.parts["performers"]:
            c = cast[perf]
            v = (c["voice"], c["pitch"], bool(c.get("backing")))
            if c["voice"] != ORIGINAL and v not in voices:
                voices.append(v)
        # full-vocal renders first: they provide the plain stems the lyric
        # alignment needs, and the lead/backing split is slow (CPU)
        voices.sort(key=lambda v: v[2])
        # stems from a render that was deleted since (e.g. an old cast's intermediates) - find fresh ones
        for k in ("stems", "backing_stems"):
            if self.proj.get(k) and not all(os.path.exists(f) for f in self.proj[k].values()):
                self.proj.pop(k)
        renders = self.proj.setdefault("renders", {})
        todo = []
        for v, p, bv in voices:
            name, d, conv = self._render_paths(v, p, bv)
            if self._adopt_render(name, d, conv):
                renders[voice_key(v, p, bv)] = conv
                if bv:
                    self.proj["backing_stems"] = self._find_backing_stems(d) or self.proj.get("backing_stems")
                # lead-only renders keep the full vocal stem too, so an all-"Keep backing" cast can still be analysed
                if not self.proj.get("stems"):
                    self.proj["stems"] = self._find_stems(d)
            else:
                todo.append((v, p, bv))
        if not self.proj.get("stems"):
            for k, conv in renders.items():
                st = self._find_stems(os.path.dirname(conv))
                if st:
                    self.proj["stems"] = st
                    break
        need_analysis = not (self.plan and self.plan.get("parts_hash") == self.proj.get("parts_hash"))
        analysis = None
        render_dir = os.path.join(self.pdir, "renders")
        os.makedirs(render_dir, exist_ok=True)

        def start_analysis(cpu):
            self.save_json_project()
            self.status("Lining the lyrics up with the recording (runs alongside the renders)…")
            t = threading.Thread(target=lambda: self._run_analysis(cpu), daemon=True)
            t.start()
            return t

        # a backing voice render still to come keeps the analysis off the GPU too
        backing_pending = bool(self.proj.get("backing_voice")) and any(bv for *_, bv in voices) \
            and not self._backing_render_ok()
        if need_analysis and self.proj.get("stems"):
            analysis = start_analysis(cpu=bool(todo) or backing_pending)
        for i, (v, p, bv) in enumerate(todo):
            name, d, conv = self._render_paths(v, p, bv)
            extra = " (lead only - splitting backing vocals on the CPU first, ~8 min)" if bv else ""
            self.status(f"Rendering voice {i + 1}/{len(todo)}: {voice_label(v)}{extra}…")
            self._clear_incomplete(d, name)
            cmd = [PIPELINE_PY, PIPELINE_SCRIPT, self.song, "--voice", v, "--f0-up-key", str(p),
                   "--index-rate", str(self.index_rate if not p else min(self.index_rate, 0.2)),
                   "--keep-intermediates", "--intermediates-dir", INTERMEDIATES_DIR,
                   "--outdir", render_dir, "--out-name", name] + (["--split-backing-vocals"] if bv else [])
            self.run_cmd(cmd, f"render {voice_label(v)}{' (lead only)' if bv else ''}")
            if not self._adopt_render(name, d, conv):
                raise RuntimeError(f"Render finished but {conv} is missing")
            renders[voice_key(v, p, bv)] = conv
            if bv:
                self.proj["backing_stems"] = self._find_backing_stems(d)
                if not self.proj["backing_stems"]:
                    raise RuntimeError(f"Backing-vocal split stems not found in {d}")
            if not self.proj.get("stems"):
                self.proj["stems"] = self._find_stems(d)
            self.save_json_project()
            if need_analysis and analysis is None and self.proj.get("stems"):
                analysis = start_analysis(cpu=i + 1 < len(todo) or backing_pending)
        if backing_pending:
            self._render_backing_voice()
        if need_analysis:
            if analysis is None:
                raise RuntimeError("No vocal stem found - render at least one character first")
            self.status("Waiting for the lyric alignment to finish…")
            analysis.join()
            if self._analysis_error:
                raise RuntimeError(self._analysis_error)
        self._do_build()

    def _backing_render_ok(self):
        br = self.proj.get("backing_render") or {}
        return br.get("voice") == self.proj.get("backing_voice") and os.path.isfile(br.get("path") or "")

    def _render_backing_voice(self):
        """Convert the split-off backing stem with the backing voice."""
        v = self.proj["backing_voice"]
        stems = self.proj.get("backing_stems")
        if not stems:
            raise RuntimeError("Backing-vocal split stems missing - can't render the backing voice")
        name = f'{slug(self.proj["name"])} [{slug(voice_label(v))} backing]'
        d = os.path.join(INTERMEDIATES_DIR, name)
        out = os.path.join(d, f"{name} (Converted Vocal).wav")
        if not os.path.isfile(out):
            self.status(f"Rendering the backing vocals as {voice_label(v)}…")
            os.makedirs(d, exist_ok=True)
            self.run_cmd([PIPELINE_PY, PIPELINE_SCRIPT, stems["backing"], "--skip-stemming", "--voice", v,
                          "--f0-up-key", "0", "--index-rate", str(self.index_rate),
                          "--outdir", d, "--out-name", name], f"render backing as {voice_label(v)}")
            if not os.path.isfile(out):
                raise RuntimeError(f"Backing render finished but {out} is missing")
        self.proj["backing_render"] = {"voice": v, "path": out}
        self.save_json_project()

    _analysis_error = None

    def _run_analysis(self, cpu=False):
        self._analysis_error = None
        try:
            self.run_cmd([RVC_PY, ENGINE, "analyze", self.pdir], "detect cuts",
                         env={"HANKIPLIER_CPU": "1"} if cpu else None)
            plan = load_json(os.path.join(self.pdir, "plan.json"), None)

            def show():
                self.plan = plan
                self._refresh_tree()
            self.ui(show)
            self.plan = plan
        except Exception as e:
            self._analysis_error = str(e)

    def save_json_project(self):
        """Thread-safe save of the project dict only (no Tk reads)."""
        if self.pdir:
            os.makedirs(self.pdir, exist_ok=True)
            save_json(os.path.join(self.pdir, "project.json"), self.proj)

    def _do_build(self):
        out = self._unique_output(self.proj.get("out_name") or self.outname_var.get() or self.proj["name"])
        self.proj["output_path"] = out
        self.save_json_project()
        if self.plan:
            save_json(os.path.join(self.pdir, "plan.json"), self.plan)
        self.status("Splicing and mixing…")
        self.run_cmd([RVC_PY, ENGINE, "build", self.pdir], "build song")
        self.proj["last_output"] = out
        self.proj.setdefault("history", []).append(
            {"output": out, "when": datetime.datetime.now().isoformat(timespec="seconds"),
             "casting": self.proj["casting"]})
        self.save_json_project()
        self.status(f"Done: {os.path.basename(out)}")
        self.log(f"Saved {out}")

    def rebuild(self):
        cast = self.current_casting()
        if cast is None:
            messagebox.showinfo("Hankiplier", "Every part needs a character first.")
            return
        # a recast voice that was never rendered needs the full Hankiply
        for c in cast.values():
            if c["voice"] != ORIGINAL and voice_key(c["voice"], c["pitch"], c.get("backing")) \
                    not in self.proj.get("renders", {}):
                self.hankiply()
                return
        self.proj["backing_voice"] = self.current_backing_voice()
        if self.proj["backing_voice"] and any(c.get("backing") for c in cast.values()) \
                and not self._backing_render_ok():
            self.hankiply()
            return
        self.proj["casting"] = cast
        self.save_project()
        self.run_worker(self._do_build)

    def reanalyze(self):
        if not messagebox.askyesno("Hankiplier", "Throw away the current cuts (including your edits) "
                                                 "and detect them again?"):
            return
        self.plan = None
        p = os.path.join(self.pdir, "plan.json")
        if os.path.exists(p):
            os.remove(p)
        self._refresh_tree()
        self.hankiply()

    @staticmethod
    def _open_path(path):
        """Open a file or folder with the desktop's default app."""
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 - Windows' own "open with default app"
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def play_output(self):
        out = self.proj.get("last_output")
        if out and os.path.exists(out):
            self._open_path(out)

    def open_folder(self):
        os.makedirs(self.output_dir, exist_ok=True)
        self._open_path(self.output_dir)

    def on_close(self):
        if self.busy() and not messagebox.askyesno("Hankiplier", "A job is running - stop it and quit?"):
            return
        try:
            self.save_project()
        except Exception:
            pass
        for p in list(self.procs):
            p.kill()
        self.destroy()


if __name__ == "__main__":
    app = App()
    if len(sys.argv) > 1:  # hankiplier SONG -> open that song's project
        app.after(200, lambda: app.set_song(os.path.abspath(sys.argv[1])))
    app.mainloop()
