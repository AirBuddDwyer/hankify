#!/usr/bin/env python3
"""Hankify: drag an MP3 in, get back a cover in the voice of your choice.

Runs the full song-pipeline (UVR5 separation -> RVC voice conversion -> mix)
against whichever RVC voice model is selected. Must run with UVR5's venv
python, since that's the only interpreter on this machine with tkinterdnd2
(drag-and-drop) installed and working.
"""
import ctypes
import datetime
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox

from tkinterdnd2 import TkinterDnD, DND_FILES
from PIL import Image, ImageTk

PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """preexec_fn: SIGKILL the pipeline subprocess the instant Hankify dies,
    by any means (clean quit, SIGTERM, SIGKILL, crash) - so killing Hankify
    mid-run can never leave an orphaned UVR5/RVC process behind. Each stage
    script the pipeline spawns in turn does the same, so the whole chain
    unwinds automatically."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


if os.name == "nt":       # no prctl on Windows (and preexec_fn isn't supported there)
    _die_with_parent = None


# Paths come from song-pipeline/hankify_config.py (env vars / ~/.config/hankifier/config.json).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "song-pipeline"))
import hankify_config as cfg  # noqa: E402

PIPELINE_SCRIPT = cfg.PIPELINE_SCRIPT
RVC_WEIGHTS_DIR = cfg.RVC_WEIGHTS_DIR
DEFAULT_VOICE_MODEL = ""  # none: the saved voice, else the first one installed
DEFAULT_INDEX_RATE = 0.95
OUTPUT_DIR = cfg.OUTPUT_DIR
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ERROR_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "errors.log")
FALLBACK_IMAGE = cfg.FALLBACK_IMAGE
THUMBNAIL_SIZE = 56
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".wav", ".mp4")
AUDIO_LABEL = "MP3/FLAC/M4A/WAV/MP4"
VOICE_EXTENSIONS = (".zip", ".pth", ".index")
VOICE_SITE = "https://voice-models.com"

# Color palettes for the Theme menu. accent_fg is the text color used on top
# of the accent color (e.g. the Go button) - it flips to dark text for the
# gold theme since gold is too light for white text to read on.
THEMES = {
    "classic": {
        "bg": "#d4d0c8", "bg_alt": "#ffffff", "fg": "#000000", "fg_dim": "#4d4d4d",
        "accent": "#0a5fd6", "accent_active": "#2f7bef", "accent_fg": "#ffffff",
        "border": "#808080",
    },
    "dark": {
        "bg": "#1e1e1e", "bg_alt": "#252526", "fg": "#f2f2f2", "fg_dim": "#c2c2c2",
        "accent": "#3a7afe", "accent_active": "#5b91ff", "accent_fg": "#ffffff",
        "border": "#4a4a4a",
    },
    "gold": {
        "bg": "#D4AF37", "bg_alt": "#C5A028", "fg": "#1a1200", "fg_dim": "#4a3a00",
        "accent": "#FFD700", "accent_active": "#FFE55C", "accent_fg": "#1a1200",
        "border": "#8B6914",
    },
    "iced_out": {
        # Black velvet display case + diamond-icy silver-blue + a gaudy
        # gold chain for the primary action button.
        "bg": "#0d1117", "bg_alt": "#1c2733", "fg": "#f0faff", "fg_dim": "#9fc5e8",
        "accent": "#FFD700", "accent_active": "#FFF275", "accent_fg": "#0d1117",
        "border": "#c7d9e8",
    },
}
ZENITY = shutil.which("zenity")
DEFAULT_THEME = "dark"
THEME_LABELS = [
    ("classic", "Classic"), ("dark", "Dark"),
    ("gold", "All Gold Everything"), ("iced_out", "Iced Out"),
]


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
    except OSError as e:
        print(e)


def log_error(song_path, message):
    """Append a failed song's full error output to ERROR_LOG_PATH. The GUI
    queue only ever holds this in memory (see App._run_queue), so this is
    the one place a failure survives after the app closes or the queue item
    gets cleared."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{stamp}] {song_path}\n{message}\n{'-' * 60}\n"
    try:
        with open(ERROR_LOG_PATH, "a") as f:
            f.write(entry)
    except OSError as e:
        print(e)


def pick_file(title, filetypes, initialdir=None):
    """Native file-open dialog. Prefers zenity (GTK/portal) over tkinter's
    built-in Tk dialog - on this Tk 9 build, that dialog's file-listing
    canvas doesn't paint an opaque background under Hyprland, leaving file
    names washed out against whatever's behind the window."""
    if ZENITY:
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        for label, pattern in filetypes:
            cmd.append(f"--file-filter={label} | {pattern}")
        if initialdir:
            cmd.append(f"--filename={os.path.join(initialdir, '')}")
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            return result.stdout.strip()
        except OSError:
            pass
    return filedialog.askopenfilename(title=title, filetypes=filetypes, initialdir=initialdir) or ""


def pick_dir(title, initialdir=None):
    """Native folder-choose dialog. See pick_file() for why zenity is preferred."""
    if ZENITY:
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if initialdir:
            cmd.append(f"--filename={os.path.join(initialdir, '')}")
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            return result.stdout.strip()
        except OSError:
            pass
    return filedialog.askdirectory(title=title, initialdir=initialdir) or ""


# Unlikely to ever appear in a real filename - safe to split zenity's
# multi-select output on without the ambiguity "|" (zenity's own default
# separator) would have.
_MULTI_SEP = "\x1f"


def pick_files(title, filetypes, initialdir=None):
    """Native multi-file-open dialog. See pick_file() for why zenity is preferred."""
    if ZENITY:
        cmd = ["zenity", "--file-selection", "--multiple", f"--separator={_MULTI_SEP}",
               f"--title={title}"]
        for label, pattern in filetypes:
            cmd.append(f"--file-filter={label} | {pattern}")
        if initialdir:
            cmd.append(f"--filename={os.path.join(initialdir, '')}")
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            out = result.stdout.strip()
            return out.split(_MULTI_SEP) if out else []
        except OSError:
            pass
    return list(filedialog.askopenfilenames(title=title, filetypes=filetypes, initialdir=initialdir) or ())


def list_voice_models():
    try:
        names = [f for f in os.listdir(RVC_WEIGHTS_DIR) if f.lower().endswith(".pth")]
    except OSError:
        return []
    return sorted(names, key=str.lower)


class Tooltip:
    """Small delayed popup shown on mouseover, styled from a (text, bg, fg)
    callable so it can pick up the current theme at show-time rather than
    whatever the theme was when the tooltip was created."""

    def __init__(self, widget, info_getter, delay_ms=400):
        self.widget = widget
        self.info_getter = info_getter
        self.delay_ms = delay_ms
        self.tip_window = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, event=None):
        self._cancel_pending()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_pending(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self.tip_window or not self.widget.winfo_ismapped():
            return
        text, bg, fg = self.info_getter()
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=text, justify="left", background=bg, foreground=fg,
            relief="solid", borderwidth=1, font=("", 9), padx=6, pady=3,
        ).pack()

    def _hide(self, event=None):
        self._cancel_pending()
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hankify")
        self.geometry("640x520")
        self.minsize(520, 420)

        self.ui_queue = queue.Queue()
        self.worker = None
        self.queue = []  # [{"path", "status": queued/working/done/error, "message", "output_path"}, ...]
        self.last_output_path = None

        self._fix_native_dialog_colors()

        self.config_data = load_config()
        self.theme_name = self.config_data.get("theme") or DEFAULT_THEME
        if self.theme_name not in THEMES:
            self.theme_name = DEFAULT_THEME
        self.palette = THEMES[self.theme_name]

        default_image = self.config_data.get("default_image") or ""
        if not default_image and FALLBACK_IMAGE and os.path.isfile(FALLBACK_IMAGE):
            default_image = FALLBACK_IMAGE
        self.default_image = default_image
        self.output_dir = self.config_data.get("output_dir") or OUTPUT_DIR

        self.voice_models = list_voice_models()
        saved_voice = self.config_data.get("voice_model")
        if saved_voice in self.voice_models:
            self.voice_model = saved_voice
        elif DEFAULT_VOICE_MODEL in self.voice_models:
            self.voice_model = DEFAULT_VOICE_MODEL
        elif self.voice_models:
            self.voice_model = self.voice_models[0]
        else:
            self.voice_model = DEFAULT_VOICE_MODEL

        self.make_mp4_var = tk.BooleanVar(value=False)
        self.keep_intermediates_var = tk.BooleanVar(value=bool(self.config_data.get("keep_intermediates")))
        self.split_backing_vocals_var = tk.BooleanVar(value=bool(self.config_data.get("split_backing_vocals")))
        self.skip_stemming_var = tk.BooleanVar(value=bool(self.config_data.get("skip_stemming")))
        self.vocal_boost_var = tk.StringVar(value=str(self.config_data.get("vocal_boost_db", "")))
        self.vocal_reverb_var = tk.BooleanVar(value=bool(self.config_data.get("vocal_reverb", True)))
        self.vocal_reverb_pct_var = tk.StringVar(value=str(self.config_data.get("vocal_reverb_percent", 8)))
        self.advanced_expanded_var = tk.BooleanVar(value=bool(self.config_data.get("advanced_expanded")))
        self.image_var = tk.StringVar(
            value=os.path.basename(self.default_image) if self.default_image else "(no image set)"
        )
        self.output_dir_var = tk.StringVar(
            value=os.path.basename(self.output_dir.rstrip("/")) or self.output_dir
        )
        self.voice_var = tk.StringVar(value=self.voice_model)
        self.subtitle_var = tk.StringVar(value=self._voice_subtitle())
        try:
            pitch_shift = int(self.config_data.get("pitch_shift", 0))
        except (TypeError, ValueError):
            pitch_shift = 0
        if not (-12 <= pitch_shift <= 12):
            pitch_shift = 0
        self.pitch_var = tk.IntVar(value=pitch_shift)
        # ttk.Scale is continuous - dragging it writes non-integer positions
        # into whatever variable it's bound to. Bind it to this separate
        # DoubleVar instead of pitch_var directly, so on_pitch_change can
        # round to the nearest semitone and snap the handle back without
        # ever exposing a fractional value through pitch_var (an IntVar,
        # which would raise if it ever picked up one mid-drag).
        self._pitch_slider_var = tk.DoubleVar(value=float(pitch_shift))
        self.pitch_label_var = tk.StringVar(value="0" if pitch_shift == 0 else f"{pitch_shift:+d}")
        try:
            index_rate = float(self.config_data.get("index_rate", DEFAULT_INDEX_RATE))
        except (TypeError, ValueError):
            index_rate = DEFAULT_INDEX_RATE
        if not (0.0 <= index_rate <= 1.0):
            index_rate = DEFAULT_INDEX_RATE
        self.index_rate_var = tk.DoubleVar(value=index_rate)
        self.index_rate_label_var = tk.StringVar(value=f"{index_rate:.2f}")
        self.status_var = tk.StringVar(value=f"Drop {AUDIO_LABEL} files or a folder to Hankify.")
        self.theme_var = tk.StringVar(value=self.theme_name)
        self.queue_label_var = tk.StringVar(value="Queue (0)")

        self._build_menu()
        self._build_style()
        self._build_ui()
        self._apply_theme()
        self._update_thumbnail(self.default_image)
        self.after(50, self._process_ui_queue)

    def _fix_native_dialog_colors(self):
        # tkinter.filedialog opens Tk's built-in (non-ttk) file chooser,
        # which our theme styling below doesn't touch at all - it's a
        # separate classic Tcl dialog, deliberately kept at fixed
        # always-readable colors regardless of the app's own theme. This is
        # safe for the rest of the app: ttk widgets ignore the classic
        # option database entirely, and the classic widgets we do use (drop
        # zone, thumbnail) set their own colors directly, which take
        # precedence over these class-level defaults.
        for widget_class in ("Listbox", "Entry", "Label", "Button", "Menu",
                              "Menubutton", "Frame", "Toplevel", "Scrollbar"):
            self.option_add(f"*{widget_class}.background", "white")
            self.option_add(f"*{widget_class}.foreground", "black")
        self.option_add("*Listbox.selectBackground", "#0a5fd6")
        self.option_add("*Listbox.selectForeground", "white")

    def _build_menu(self):
        menubar = tk.Menu(self)
        theme_menu = tk.Menu(menubar, tearoff=0)
        for key, label in THEME_LABELS:
            theme_menu.add_radiobutton(
                label=label, variable=self.theme_var, value=key,
                command=lambda k=key: self.set_theme(k),
            )
        menubar.add_cascade(label="Theme", menu=theme_menu)
        self.config(menu=menubar)
        self.menubar = menubar
        self.theme_menu = theme_menu

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        # IMPORTANT: never configure the base ttk classes here (".",
        # "TButton", "TLabel", "TEntry", ...). ttk styling is global to the
        # whole process, and Tk's built-in file dialog (tkfbox.tcl) uses
        # ttk::entry/ttk::button internally too - overriding the base
        # classes previously made that dialog's own filename entry
        # unreadable. Use custom-named styles for everything in this window
        # instead, so only our own widgets are affected. This is called
        # again on every theme switch, and ttk widgets using these style
        # names re-render automatically when it's re-configured.
        p = self.palette
        style.configure("Hankify.TFrame", background=p["bg"])
        style.configure("Hankify.TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("Hankify.Dim.TLabel", background=p["bg"], foreground=p["fg_dim"], font=("", 10))
        style.configure("Hankify.Horizontal.TProgressbar", background=p["accent"], troughcolor=p["bg_alt"],
                         bordercolor=p["bg"], lightcolor=p["accent"], darkcolor=p["accent"])
        style.configure("Hankify.Horizontal.TScale", background=p["bg"], troughcolor=p["bg_alt"],
                         bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"])
        style.map("Hankify.Horizontal.TScale", background=[("active", p["bg"])])
        style.configure("Hankify.TCheckbutton", background=p["bg"], foreground=p["fg"], font=("", 10))
        style.map("Hankify.TCheckbutton", background=[("active", p["bg"])])
        style.configure("Hankify.TRadiobutton", background=p["bg"], foreground=p["fg"], font=("", 10))
        style.map("Hankify.TRadiobutton", background=[("active", p["bg"])])
        style.configure("Hankify.TButton", background=p["bg_alt"], foreground=p["fg"],
                         bordercolor=p["border"], focuscolor=p["bg_alt"], padding=3)
        style.map("Hankify.TButton", background=[("active", p["border"])])
        style.configure("Hankify.Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
                         bordercolor=p["accent"], focuscolor=p["accent"], padding=6, font=("", 11, "bold"))
        style.map("Hankify.Accent.TButton",
                  background=[("disabled", p["bg_alt"]), ("active", p["accent_active"])],
                  foreground=[("disabled", p["fg_dim"])])
        style.configure("Hankify.TEntry", fieldbackground=p["bg_alt"], foreground=p["fg"],
                         bordercolor=p["border"], insertcolor=p["fg"])
        style.configure("Hankify.TCombobox", fieldbackground=p["bg_alt"], background=p["bg_alt"],
                         foreground=p["fg"], bordercolor=p["border"], arrowcolor=p["fg"])
        style.map("Hankify.TCombobox",
                  fieldbackground=[("readonly", p["bg_alt"])],
                  foreground=[("readonly", p["fg"])],
                  selectbackground=[("readonly", p["bg_alt"])],
                  selectforeground=[("readonly", p["fg"])])

    def _build_ui(self):
        left = ttk.Frame(self, style="Hankify.TFrame")
        left.pack(side="left", fill="y", padx=(10, 6), pady=10)

        right = ttk.Frame(self, style="Hankify.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        ttk.Label(
            left, text="Hankify", font=("", 16, "bold"), style="Hankify.TLabel"
        ).pack(pady=(0, 0))
        ttk.Label(
            left, textvariable=self.subtitle_var, style="Hankify.Dim.TLabel"
        ).pack(pady=(0, 6))

        self.drop_zone = tk.Label(
            left,
            text=f"Drop {AUDIO_LABEL} files or a folder\nor click to browse  (voice .zip files go here too)",
            justify="center",
            font=("", 12, "bold"),
            relief="ridge", borderwidth=2,
            highlightthickness=1,
        )
        self.drop_zone.pack(fill="x", padx=12, pady=(6, 2), ipady=16)
        self.drop_zone.bind("<Button-1>", lambda e: self.browse_files())

        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>", self.on_drop)

        ttk.Button(
            left, text="Add Folder...", command=self.browse_folder, style="Hankify.TButton",
        ).pack(fill="x", padx=12, pady=(0, 6))

        voice_row = ttk.Frame(left, style="Hankify.TFrame")
        voice_row.pack(fill="x", padx=14, pady=(6, 2))
        ttk.Label(voice_row, text="Voice:", style="Hankify.Dim.TLabel").pack(side="left")
        self.voice_combo = ttk.Combobox(
            voice_row, textvariable=self.voice_var, values=self.voice_models,
            state="readonly", style="Hankify.TCombobox",
        )
        self.voice_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.voice_combo.bind("<<ComboboxSelected>>", self.on_voice_change)

        voice_btns = ttk.Frame(left, style="Hankify.TFrame")
        voice_btns.pack(fill="x", padx=12, pady=(2, 4))
        import_btn = ttk.Button(voice_btns, text="Import Voice...", command=self.browse_voices,
                                style="Hankify.TButton")
        import_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        find_btn = ttk.Button(voice_btns, text="Find Voices Online", style="Hankify.TButton",
                              command=lambda: webbrowser.open(VOICE_SITE))
        find_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))
        Tooltip(import_btn, lambda: (
            "Pick a downloaded voice .zip (or its .pth + .index files).\n"
            "Hankify puts the files where they belong. You can also drop the zip on the box above.",
            self.palette["bg_alt"], self.palette["fg"]))
        Tooltip(find_btn, lambda: (
            f"Opens {VOICE_SITE} in your browser: search a character's name,\n"
            "download the zip, then click Import Voice...",
            self.palette["bg_alt"], self.palette["fg"]))

        pitch_row = ttk.Frame(left, style="Hankify.TFrame")
        pitch_row.pack(fill="x", padx=14, pady=(2, 2))
        ttk.Label(pitch_row, text="Pitch:", style="Hankify.Dim.TLabel").pack(side="left")
        self.pitch_scale = ttk.Scale(
            pitch_row, from_=-12, to=12, variable=self._pitch_slider_var,
            orient="horizontal", command=self.on_pitch_change,
            style="Hankify.Horizontal.TScale",
        )
        self.pitch_scale.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Label(
            pitch_row, textvariable=self.pitch_label_var, style="Hankify.Dim.TLabel", width=4,
        ).pack(side="left")

        index_rate_row = ttk.Frame(left, style="Hankify.TFrame")
        index_rate_row.pack(fill="x", padx=14, pady=(2, 2))
        ttk.Label(index_rate_row, text="Index Rate:", style="Hankify.Dim.TLabel").pack(side="left")
        self.index_rate_scale = ttk.Scale(
            index_rate_row, from_=0.0, to=1.0, variable=self.index_rate_var,
            orient="horizontal", command=self.on_index_rate_change,
            style="Hankify.Horizontal.TScale",
        )
        self.index_rate_scale.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Label(
            index_rate_row, textvariable=self.index_rate_label_var, style="Hankify.Dim.TLabel", width=4,
        ).pack(side="left")
        Tooltip(
            self.index_rate_scale,
            lambda: (
                "How strongly the voice model's timbre is applied.\n"
                "Higher = closer to the model, lower = closer to the original singer.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        self.advanced_toggle_btn = ttk.Button(
            left, text=self._advanced_toggle_text(), style="Hankify.TButton",
            command=self.toggle_advanced,
        )
        self.advanced_toggle_btn.pack(fill="x", padx=14, pady=(4, 0))

        self.advanced_frame = ttk.Frame(left, style="Hankify.TFrame")

        self.make_mp4_check = ttk.Checkbutton(
            self.advanced_frame, text="Also make MP4", variable=self.make_mp4_var,
            style="Hankify.TCheckbutton",
        )
        self.make_mp4_check.pack(anchor="w", padx=14, pady=(4, 0))

        self.keep_intermediates_check = ttk.Checkbutton(
            self.advanced_frame, text="Keep intermediate files", variable=self.keep_intermediates_var,
            style="Hankify.TCheckbutton", command=self.on_keep_intermediates_change,
        )
        self.keep_intermediates_check.pack(anchor="w", padx=14, pady=(2, 0))
        Tooltip(
            self.keep_intermediates_check,
            lambda: (
                "Saves this run's UVR5 vocal/instrumental split to disk, so a\n"
                "later Hankify run on the same song reuses it instead of\n"
                "separating it again. E.g. checking this the first time you\n"
                "Hankify a song, then trying it with 4 more voices, only runs\n"
                "that song through UVR5 once instead of five times.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        self.split_backing_vocals_check = ttk.Checkbutton(
            self.advanced_frame, text="Preserve backing vocals (convert lead only)",
            variable=self.split_backing_vocals_var,
            style="Hankify.TCheckbutton", command=self.on_split_backing_vocals_change,
        )
        self.split_backing_vocals_check.pack(anchor="w", padx=14, pady=(2, 0))
        Tooltip(
            self.split_backing_vocals_check,
            lambda: (
                "Splits the vocal stem into lead and backing vocals (UVR5's\n"
                "karaoke model), converts only the lead vocal, and mixes the\n"
                "original backing vocals back in untouched.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        vocal_boost_row = ttk.Frame(self.advanced_frame, style="Hankify.TFrame")
        vocal_boost_row.pack(anchor="w", padx=14, pady=(2, 0))
        ttk.Label(vocal_boost_row, text="Vocal boost (dB):", style="Hankify.Dim.TLabel").pack(side="left")
        self.vocal_boost_entry = ttk.Entry(vocal_boost_row, textvariable=self.vocal_boost_var, width=6,
                                           style="Hankify.TEntry")
        self.vocal_boost_entry.pack(side="left", padx=(6, 0))
        ttk.Label(vocal_boost_row, text="blank = auto", style="Hankify.Dim.TLabel").pack(side="left", padx=(6, 0))
        self.vocal_boost_var.trace_add("write", lambda *_: self.on_vocal_boost_change())

        vocal_reverb_row = ttk.Frame(self.advanced_frame, style="Hankify.TFrame")
        vocal_reverb_row.pack(anchor="w", padx=14, pady=(2, 0))
        self.vocal_reverb_check = ttk.Checkbutton(
            vocal_reverb_row, text="Add vocal reverb", variable=self.vocal_reverb_var,
            style="Hankify.TCheckbutton", command=self.on_vocal_reverb_change,
        )
        self.vocal_reverb_check.pack(side="left")
        ttk.Spinbox(vocal_reverb_row, from_=8, to=12, increment=1, width=3,
                    textvariable=self.vocal_reverb_pct_var, state="readonly").pack(side="left", padx=(6, 0))
        ttk.Label(vocal_reverb_row, text="%", style="Hankify.Dim.TLabel").pack(side="left", padx=(2, 0))
        self.vocal_reverb_pct_var.trace_add("write", lambda *_: self.on_vocal_reverb_change())
        Tooltip(
            self.vocal_reverb_check,
            lambda: (
                "Short-tail reverb (~0.8 s) on the converted vocal, low in\n"
                "the mix: 8-12% of the dry vocal's level. Leave off for a dry vocal.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )
        Tooltip(
            self.vocal_boost_entry,
            lambda: (
                "How much to raise the converted vocal (after EQ + compression)\n"
                "before mixing. Blank = auto: match the original singer's loudness.\n"
                "Type a number (e.g. 12) to use exactly that many dB instead.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        self.skip_stemming_check = ttk.Checkbutton(
            self.advanced_frame, text="Skip stemming (input is already a vocal stem)",
            variable=self.skip_stemming_var,
            style="Hankify.TCheckbutton", command=self.on_skip_stemming_change,
        )
        self.skip_stemming_check.pack(anchor="w", padx=14, pady=(2, 0))
        Tooltip(
            self.skip_stemming_check,
            lambda: (
                "For when you've already separated the vocal yourself: skips\n"
                "UVR5 entirely and converts the dropped file as-is. Since\n"
                "there's no instrumental to mix back in, this outputs just\n"
                "the converted vocal (a .wav) instead of a finished MP3, so\n"
                "\"Preserve backing vocals\" and \"Also make MP4\" are disabled\n"
                "while this is checked.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        if self.advanced_expanded_var.get():
            self.advanced_frame.pack(fill="x")
        self._apply_skip_stemming_lock()

        image_row = ttk.Frame(left, style="Hankify.TFrame")
        self.image_row = image_row
        image_row.pack(fill="x", padx=14, pady=(0, 6))

        self.thumb_frame = tk.Frame(
            image_row, width=THUMBNAIL_SIZE, height=THUMBNAIL_SIZE, highlightthickness=1,
        )
        self.thumb_frame.pack(side="left")
        self.thumb_frame.pack_propagate(False)
        self.thumbnail_label = tk.Label(self.thumb_frame, font=("", 8), justify="center")
        self.thumbnail_label.pack(fill="both", expand=True)

        image_info = ttk.Frame(image_row, style="Hankify.TFrame")
        image_info.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(
            image_info, textvariable=self.image_var, style="Hankify.Dim.TLabel", wraplength=170
        ).pack(anchor="w")
        ttk.Button(
            image_info, text="Change...", command=self.choose_default_image, style="Hankify.TButton"
        ).pack(anchor="w", pady=(4, 0))

        output_row = ttk.Frame(left, style="Hankify.TFrame")
        output_row.pack(fill="x", padx=14, pady=(0, 6))
        ttk.Label(output_row, text="Output:", style="Hankify.Dim.TLabel").pack(side="left")
        ttk.Label(
            output_row, textvariable=self.output_dir_var, style="Hankify.Dim.TLabel"
        ).pack(side="left", padx=(4, 0), fill="x", expand=True)
        ttk.Button(
            output_row, text="Browse...", command=self.choose_output_dir, style="Hankify.TButton"
        ).pack(side="right")

        self.go_button = ttk.Button(
            left, text="Start Queue", command=self.on_go_click,
            style="Hankify.Accent.TButton", state="disabled",
        )
        self.go_button.pack(fill="x", padx=12, pady=(2, 4))

        self.show_file_button = ttk.Button(
            left, text="Show File", command=self.open_output_location,
            style="Hankify.TButton", state="disabled",
        )
        self.show_file_button.pack(fill="x", padx=12, pady=(0, 4))

        ttk.Label(
            left, textvariable=self.status_var, wraplength=270,
            font=("", 10), justify="center", style="Hankify.TLabel",
        ).pack(fill="x", padx=10, pady=(2, 4))

        self.progress = ttk.Progressbar(
            left, mode="indeterminate", style="Hankify.Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", padx=12, pady=(0, 6))

        # The queue lives in its own column on the right, spanning the full
        # window height, so a long queue has real room instead of fighting
        # the settings column for a sliver of space at the bottom.
        queue_header = ttk.Frame(right, style="Hankify.TFrame")
        queue_header.pack(fill="x", pady=(0, 2))
        ttk.Label(
            queue_header, textvariable=self.queue_label_var, style="Hankify.Dim.TLabel"
        ).pack(side="left")
        clear_button = ttk.Button(
            queue_header, text="Clear", command=self.clear_queue, style="Hankify.TButton",
        )
        clear_button.pack(side="right")
        Tooltip(
            clear_button,
            lambda: (
                "Drops everything not yet started.\n"
                "If a song is mid-conversion, it finishes and then the queue stops.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        remove_button = ttk.Button(
            queue_header, text="Remove Selected", command=self.remove_selected_queue_items,
            style="Hankify.TButton",
        )
        remove_button.pack(side="right", padx=(0, 6))

        # Packed at the bottom *before* the queue list so the list's
        # expand=True can't squeeze it out of a short window.
        multivoice_button = ttk.Button(
            right, text="Multi-voice...", command=self.open_multivoice_dialog,
            style="Hankify.TButton",
        )
        multivoice_button.pack(side="bottom", fill="x", pady=(4, 0))
        Tooltip(
            multivoice_button,
            lambda: (
                "Give the selected song a different voice per section\n"
                "(e.g. one voice per rapper). Each voice is converted\n"
                "separately and spliced at the start times you enter,\n"
                "nudged up to 0.5s to land in a gap between words.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )
        Tooltip(
            remove_button,
            lambda: (
                "Removes the selected song(s) from the queue.\n"
                "Ctrl/Shift-click to select more than one.\n"
                "Same as pressing Delete, or double-clicking a single song.",
                self.palette["bg_alt"], self.palette["fg"],
            ),
        )

        queue_frame = tk.Frame(right, highlightthickness=1)
        queue_frame.pack(fill="both", expand=True)
        queue_scroll = ttk.Scrollbar(queue_frame, orient="vertical")
        self.queue_listbox = tk.Listbox(
            queue_frame, activestyle="none", font=("", 10), selectmode="extended",
            yscrollcommand=queue_scroll.set, borderwidth=0, highlightthickness=0,
        )
        queue_scroll.configure(command=self.queue_listbox.yview)
        self.queue_listbox.pack(side="left", fill="both", expand=True)
        queue_scroll.pack(side="right", fill="y")
        self.queue_listbox.bind("<Double-Button-1>", self.on_queue_item_double_click)
        self.queue_listbox.bind("<Delete>", self.remove_selected_queue_items)
        self.queue_listbox.bind("<BackSpace>", self.remove_selected_queue_items)

    # --- theming ---

    def set_theme(self, name):
        if name not in THEMES:
            return
        self.theme_name = name
        self.palette = THEMES[name]
        self.theme_var.set(name)
        self.config_data["theme"] = name
        save_config(self.config_data)
        self._apply_theme()

    def _apply_theme(self):
        p = self.palette
        self.configure(bg=p["bg"])
        self._build_style()

        self.drop_zone.configure(bg=p["bg_alt"], fg=p["fg"], highlightbackground=p["border"])
        self.thumb_frame.configure(bg=p["bg_alt"], highlightbackground=p["border"])
        self.thumbnail_label.configure(bg=p["bg_alt"], fg=p["fg_dim"])

        self.queue_listbox.master.configure(bg=p["border"], highlightbackground=p["border"])
        self.queue_listbox.configure(
            bg=p["bg_alt"], fg=p["fg"],
            selectbackground=p["accent"], selectforeground=p["accent_fg"],
        )
        self._refresh_queue_list()

        for menu in (self.menubar, self.theme_menu):
            menu.configure(
                bg=p["bg_alt"], fg=p["fg"],
                activebackground=p["accent"], activeforeground=p["accent_fg"],
            )

    # --- thread-safe UI dispatch (background pipeline run never touches Tk) ---

    def _process_ui_queue(self):
        try:
            while True:
                func = self.ui_queue.get_nowait()
                try:
                    func()
                except Exception as e:
                    print(e)
        except queue.Empty:
            pass
        self.after(50, self._process_ui_queue)

    def _run_on_main(self, func):
        self.ui_queue.put(func)

    # --- input handling ---

    def _voice_subtitle(self):
        return f'-> "{os.path.splitext(self.voice_model)[0]}"'

    def on_voice_change(self, event=None):
        chosen = self.voice_var.get()
        if not chosen:
            return
        self.voice_model = chosen
        self.subtitle_var.set(self._voice_subtitle())
        self.config_data["voice_model"] = chosen
        save_config(self.config_data)

    def on_pitch_change(self, value=None):
        snapped = max(-12, min(12, round(float(value))))
        if self._pitch_slider_var.get() != float(snapped):
            self._pitch_slider_var.set(float(snapped))  # snap the handle to the nearest semitone
        if self.pitch_var.get() != snapped:
            self.pitch_var.set(snapped)
        self.pitch_label_var.set("0" if snapped == 0 else f"{snapped:+d}")
        self.config_data["pitch_shift"] = snapped
        save_config(self.config_data)

    def on_index_rate_change(self, value=None):
        rate = round(self.index_rate_var.get(), 2)
        self.index_rate_label_var.set(f"{rate:.2f}")
        self.config_data["index_rate"] = rate
        save_config(self.config_data)

    def on_keep_intermediates_change(self):
        self.config_data["keep_intermediates"] = self.keep_intermediates_var.get()
        save_config(self.config_data)

    def on_split_backing_vocals_change(self):
        self.config_data["split_backing_vocals"] = self.split_backing_vocals_var.get()
        save_config(self.config_data)

    def on_vocal_boost_change(self):
        self.config_data["vocal_boost_db"] = self.vocal_boost_var.get().strip()
        save_config(self.config_data)

    def on_vocal_reverb_change(self):
        self.config_data["vocal_reverb"] = self.vocal_reverb_var.get()
        self.config_data["vocal_reverb_percent"] = self.vocal_reverb_pct_var.get()
        save_config(self.config_data)

    def _vocal_reverb_pct(self):
        """Reverb percent if the checkbox is on, else 0 (explicit off - reverb is on by default in the pipeline)."""
        if not self.vocal_reverb_var.get():
            return 0.0
        try:
            return float(self.vocal_reverb_pct_var.get())
        except ValueError:
            return 10.0

    def _vocal_boost_db(self):
        """The Vocal boost box as a float, or None for blank/unparseable (= auto)."""
        try:
            return float(self.vocal_boost_var.get().strip().replace("+", ""))
        except ValueError:
            return None

    def on_skip_stemming_change(self):
        self.config_data["skip_stemming"] = self.skip_stemming_var.get()
        save_config(self.config_data)
        self._apply_skip_stemming_lock()

    def _apply_skip_stemming_lock(self):
        # Skip-stemming outputs just a converted vocal .wav (no instrumental
        # to mix back in, so no final MP3) - these two options both depend
        # on a finished MP3 existing, so they're meaningless (and, for
        # split-backing-vocals, actively rejected by pipeline.py) while it's
        # checked.
        if self.skip_stemming_var.get():
            self.split_backing_vocals_var.set(False)
            self.make_mp4_var.set(False)
            self.split_backing_vocals_check.configure(state="disabled")
            self.make_mp4_check.configure(state="disabled")
        else:
            self.split_backing_vocals_check.configure(state="normal")
            self.make_mp4_check.configure(state="normal")

    def _advanced_toggle_text(self):
        return ("− Advanced settings" if self.advanced_expanded_var.get()
                else "+ Advanced settings")

    def toggle_advanced(self):
        expanded = not self.advanced_expanded_var.get()
        self.advanced_expanded_var.set(expanded)
        self.config_data["advanced_expanded"] = expanded
        save_config(self.config_data)
        self.advanced_toggle_btn.configure(text=self._advanced_toggle_text())
        if expanded:
            self.advanced_frame.pack(fill="x", before=self.image_row)
        else:
            self.advanced_frame.pack_forget()

    def _update_thumbnail(self, path):
        photo = None
        if path and os.path.isfile(path):
            try:
                img = Image.open(path)
                img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
                photo = ImageTk.PhotoImage(img)
            except Exception as e:
                print(e)
        if photo:
            self.thumbnail_label.configure(image=photo, text="")
        else:
            self.thumbnail_label.configure(image="", text="no\nimage")
        # Tkinter doesn't keep PhotoImage objects alive on its own - without
        # this reference the image would get garbage-collected and vanish.
        self.thumbnail_label.image = photo

    def choose_default_image(self):
        chosen = pick_file(
            "Choose default image for MP4s",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All files", "*")],
        )
        if chosen:
            self.default_image = chosen
            self.image_var.set(os.path.basename(chosen))
            self.config_data["default_image"] = chosen
            save_config(self.config_data)
            self._update_thumbnail(chosen)

    def choose_output_dir(self):
        chosen = pick_dir(
            "Choose output folder", initialdir=self.output_dir or OUTPUT_DIR
        )
        if chosen:
            self.output_dir = chosen
            self.output_dir_var.set(os.path.basename(chosen.rstrip("/")) or chosen)
            self.config_data["output_dir"] = chosen
            save_config(self.config_data)

    def browse_files(self):
        chosen = pick_files(
            f"Choose one or more {AUDIO_LABEL} files",
            filetypes=[(AUDIO_LABEL, "*.mp3 *.flac *.m4a *.wav *.mp4"), ("All files", "*")],
        )
        if chosen:
            self._add_paths_to_queue(chosen)

    def browse_folder(self):
        chosen_dir = pick_dir(f"Choose a folder of {AUDIO_LABEL} files")
        if not chosen_dir:
            return
        self._add_paths_to_queue(self._audio_files_in(chosen_dir))

    def browse_voices(self):
        chosen = pick_files(
            "Choose a downloaded voice (.zip, or a .pth with its .index)",
            filetypes=[("Voice files", "*.zip *.pth *.index"), ("All files", "*")],
            initialdir=os.path.join(os.path.expanduser("~"), "Downloads"),
        )
        if chosen:
            self.import_voices(chosen)

    def import_voices(self, paths):
        import voice_import  # song-pipeline/, on sys.path above
        self.status_var.set("Importing voice...")
        self.update_idletasks()
        try:
            results, problems = voice_import.import_paths(paths)
        except Exception as e:  # noqa: BLE001 - show it, don't crash the GUI
            log_error(paths[0], f"voice import failed: {e}")
            self.status_var.set(f"Voice import failed: {e}")
            return
        self.voice_models = list_voice_models()
        self.voice_combo.configure(values=self.voice_models)
        if results:
            self.voice_var.set(results[-1]["voice"])
            self.on_voice_change()
        lines = voice_import.summary(results, problems)
        if len(results) == 1 and not problems:
            r = results[0]
            name = os.path.splitext(r["voice"])[0]
            self.status_var.set(f'Voice "{name}" {r["status"]} and selected.'
                                + ("" if r["index"] else " (it came without an .index file; that's OK)"))
        else:
            self.status_var.set(f"Imported {len(results)} voice(s)." if results else "No voice imported.")
            messagebox.showinfo("Import Voice", "\n".join(lines) or "Nothing to import.", parent=self)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        voices = [p for p in paths if os.path.isfile(p) and p.lower().endswith(VOICE_EXTENSIONS)]
        if voices:
            self.import_voices(voices)
            paths = [p for p in paths if p not in voices]
            if not paths:
                return
        collected = []
        for p in paths:
            if os.path.isdir(p):
                collected.extend(self._audio_files_in(p))
            elif p.lower().endswith(AUDIO_EXTENSIONS):
                collected.append(p)
        if not collected:
            self.status_var.set(f"That doesn't look like an {AUDIO_LABEL} file or folder.")
            return
        self._add_paths_to_queue(collected)

    def _audio_files_in(self, folder):
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as e:
            self.status_var.set(f"Couldn't read that folder: {e}")
            return []
        return [os.path.join(folder, n) for n in names if n.lower().endswith(AUDIO_EXTENSIONS)]

    # --- queue ---

    def _add_paths_to_queue(self, paths):
        existing = {it["path"] for it in self.queue}
        added = 0
        for p in paths:
            if p in existing:
                continue
            self.queue.append({"path": p, "status": "queued"})
            existing.add(p)
            added += 1
        if added:
            self.status_var.set(f"Added {added} file{'s' if added != 1 else ''} to the queue.")
        elif paths:
            self.status_var.set("Already in the queue.")
        else:
            self.status_var.set(f"No {AUDIO_LABEL} files found there.")
        self._refresh_queue_list()
        self._update_go_button_state()

    def clear_queue(self):
        # Reassigning self.queue (rather than self.queue.clear()) keeps the
        # in-flight "working" item's dict alive under whatever reference the
        # worker thread already has, while dropping every item it hasn't
        # reached yet - so this is safe to call mid-run and doubles as an
        # "abort after the current song" button: with nothing left queued,
        # _run_queue's next scan finds no more work and stops on its own.
        self.queue = [it for it in self.queue if it["status"] == "working"]
        self._refresh_queue_list()
        self._update_go_button_state()

    def _remove_queue_indices(self, indices):
        # Only "queued"/"error" rows are ever deleted here - never "working".
        # That's what makes this safe to call while the worker thread is
        # alive: it only ever touches the one item it's currently on (see
        # clear_queue), so leaving that row alone avoids racing it.
        removed = False
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(self.queue) and self.queue[index]["status"] in ("queued", "error"):
                del self.queue[index]
                removed = True
        if removed:
            self._refresh_queue_list()
            self._update_go_button_state()

    def on_queue_item_double_click(self, event):
        self._remove_queue_indices([self.queue_listbox.nearest(event.y)])

    def remove_selected_queue_items(self, event=None):
        self._remove_queue_indices(self.queue_listbox.curselection())

    # --- multi-voice ---

    def open_multivoice_dialog(self):
        selection = self.queue_listbox.curselection()
        if len(selection) != 1:
            self.status_var.set("Select one song in the queue to set up multi-voice.")
            return
        item = self.queue[selection[0]]
        if item["status"] not in ("queued", "error"):
            self.status_var.set("That song is already running.")
            return
        MultiVoiceDialog(self, item)

    def _set_item_sections(self, item, sections):
        item["sections"] = sections
        if item["status"] == "error":
            item["status"] = "queued"
        self._refresh_queue_list()
        self._update_go_button_state()

    def _refresh_queue_list(self):
        p = self.palette
        icons = {"queued": "⏳", "working": "▶", "done": "✓", "error": "✗"}
        colors = {
            "queued": p["fg"], "working": p["accent"],
            "done": "#2ecc71", "error": "#e74c3c",
        }
        self.queue_listbox.delete(0, "end")
        for it in self.queue:
            voices = len({s["voice"] for s in it.get("sections") or []})
            tag = f"[{voices} voices] " if it.get("sections") else ""
            self.queue_listbox.insert("end", f"{icons[it['status']]} {tag}{os.path.basename(it['path'])}")
        for i, it in enumerate(self.queue):
            self.queue_listbox.itemconfig(i, fg=colors[it["status"]])
        self.queue_label_var.set(f"Queue ({len(self.queue)})")

    def _update_go_button_state(self):
        busy = bool(self.worker and self.worker.is_alive())
        has_queued = any(it["status"] == "queued" for it in self.queue)
        self.go_button.configure(state="normal" if (has_queued and not busy) else "disabled")

    # --- run ---

    def on_go_click(self):
        if self.advanced_expanded_var.get():
            self.toggle_advanced()
        if self.worker and self.worker.is_alive():
            return
        make_mp4 = self.make_mp4_var.get()
        if make_mp4 and not (self.default_image and os.path.isfile(self.default_image)):
            self.status_var.set('No valid image set. Click "Change..." to pick one for the MP4.')
            return
        if not any(it["status"] == "queued" for it in self.queue):
            return
        if self.voice_model not in list_voice_models():
            self.status_var.set('No voices installed yet. See "ADDING VOICES" in "README - START HERE.txt".')
            return

        self.go_button.configure(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(
            target=self._run_queue,
            args=(make_mp4, self.default_image, self.output_dir,
                  self.voice_model, self.pitch_var.get(), round(self.index_rate_var.get(), 2),
                  self.keep_intermediates_var.get(), self.split_backing_vocals_var.get(),
                  self.skip_stemming_var.get(), self._vocal_boost_db(), self._vocal_reverb_pct()),
            daemon=True,
        )
        self.worker.start()

    def _unique_out_name(self, output_dir, base_name):
        """Base filename (no extension) that doesn't collide with an existing
        .mp3, .mp4, or skip-stemming .wav output in output_dir, appending
        " (1)", " (2)", ... as needed."""
        candidate = base_name
        n = 1
        while (os.path.exists(os.path.join(output_dir, candidate + ".mp3"))
               or os.path.exists(os.path.join(output_dir, candidate + ".mp4"))
               or os.path.exists(os.path.join(output_dir, f"{candidate} (Converted Vocal).wav"))):
            candidate = f"{base_name} ({n})"
            n += 1
        return candidate

    def _run_queue(self, make_mp4, image_path, output_dir, voice_model, pitch_shift, index_rate,
                   keep_intermediates, split_backing_vocals, skip_stemming,
                   vocal_boost_db=None, vocal_reverb_pct=None):
        # Re-scanning self.queue for the next "queued" item on each pass
        # (rather than iterating it directly) means files added mid-run get
        # picked up, and a completed item can be removed outright the moment
        # it succeeds - the queue is for songs still waiting, not a log of
        # everything ever run through it. Failed items stay (marked ✗) so
        # there's something to investigate; the UI only allows removing them
        # while no worker is alive (see on_queue_item_double_click/clear_queue).
        while True:
            item = next((it for it in self.queue if it["status"] == "queued"), None)
            if item is None:
                break
            item["status"] = "working"
            self._run_on_main(self._refresh_queue_list)
            name = os.path.basename(item["path"])
            position = self.queue.index(item) + 1
            self._run_on_main(
                lambda n=name, i=position, t=len(self.queue): self.status_var.set(
                    f"Hankifying {n}... ({i}/{t})"
                )
            )
            ok, message, output_path = self._process_one(
                item["path"], make_mp4, image_path, output_dir, voice_model, pitch_shift, index_rate,
                keep_intermediates, split_backing_vocals, skip_stemming, item.get("sections"),
                vocal_boost_db, vocal_reverb_pct,
            )
            if ok:
                self.queue.remove(item)
                self._run_on_main(lambda n=name, m=message, p=output_path: self._item_succeeded(n, m, p))
            else:
                item["status"] = "error"
                item["message"] = message
                item["output_path"] = output_path
                self._run_on_main(self._refresh_queue_list)

        self._run_on_main(self._finish_queue)

    def _item_succeeded(self, name, message, output_path):
        self.status_var.set(f"{name}: {message}")
        self.last_output_path = output_path if output_path and os.path.exists(output_path) else None
        self.show_file_button.configure(state="normal" if self.last_output_path else "disabled")
        self._refresh_queue_list()

    def _extract_audio_from_video(self, video_path):
        """Pull the audio track out of a video file (e.g. a dropped MP4) into
        a standalone WAV, so the rest of the pipeline - which only ever deals
        with plain audio - never has to know the input was a video."""
        fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="hankify_extract_")
        os.close(fd)
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", tmp_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            tail = "\n".join(result.stdout.strip().splitlines()[-15:])
            return None, tail
        return tmp_path, None

    def _process_one(self, input_path, make_mp4, image_path, output_dir, voice_model, pitch_shift,
                      index_rate, keep_intermediates=False, split_backing_vocals=False,
                      skip_stemming=False, sections=None, vocal_boost_db=None,
                      vocal_reverb_pct=None):
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        out_name = self._unique_out_name(output_dir, base_name)

        # A dropped MP4 is a video container - the pipeline (UVR5/RVC) only
        # understands plain audio, so pull the audio track out first and run
        # the pipeline on that instead. The video's own picture is discarded;
        # if "Also make MP4" is checked, the output gets the selected default
        # picture instead, same as any other audio input.
        extracted_audio = None
        pipeline_input = input_path
        if input_path.lower().endswith(".mp4"):
            self._run_on_main(
                lambda: self.status_var.set(f"Extracting audio from {os.path.basename(input_path)}...")
            )
            extracted_audio, error = self._extract_audio_from_video(input_path)
            if extracted_audio is None:
                message = f"Couldn't extract audio from MP4:\n{error}"
                log_error(input_path, message)
                return False, message, None
            pipeline_input = extracted_audio

        cmd = [
            sys.executable if os.name == "nt" else "python3", PIPELINE_SCRIPT, pipeline_input,
            "--voice", voice_model,
            "--outdir", output_dir,
            "--out-name", out_name,
            "--f0-up-key", str(pitch_shift),
            "--index-rate", str(index_rate),
        ]
        # Always pass --intermediates-dir (regardless of the checkbox) so a
        # separation kept from a previous run - possibly for a different
        # voice - can be found and reused, skipping UVR5 entirely. Only
        # --keep-intermediates controls whether *this* run's own separation
        # gets saved there afterward for future reuse.
        cmd += ["--intermediates-dir", os.path.join(output_dir, "intermediates")]
        if keep_intermediates:
            cmd += ["--keep-intermediates"]
        if skip_stemming:
            cmd += ["--skip-stemming"]
        elif split_backing_vocals:
            cmd += ["--split-backing-vocals"]
        if vocal_boost_db is not None:
            cmd += ["--vocal-boost-db", str(vocal_boost_db)]
        if vocal_reverb_pct is not None:
            cmd += ["--vocal-reverb", str(vocal_reverb_pct)]
        sections_file = None
        if sections:
            fd, sections_file = tempfile.mkstemp(suffix=".json", prefix="hankify_sections_")
            with os.fdopen(fd, "w") as f:
                json.dump(sections, f)
            cmd += ["--sections", sections_file]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=_die_with_parent
            )
        except Exception as e:
            message = f"Couldn't start the pipeline: {e}"
            log_error(input_path, message)
            return False, message, None
        finally:
            for tmp in (extracted_audio, sections_file):
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        # Multi-voice runs report where each cut actually landed (after
        # snapping to a quiet gap) - surface that so it can be fine-tuned.
        cut_note = ""
        if sections:
            cuts = [line.strip()[len("cut at "):].split(" (asked")[0]
                    for line in result.stdout.splitlines() if line.strip().startswith("cut at ")]
            if cuts:
                cut_note = f" (cuts at {', '.join(cuts)})"

        if result.returncode != 0:
            full_output = result.stdout.strip()
            log_error(input_path, full_output)
            tail = "\n".join(full_output.splitlines()[-15:])
            return False, f"Failed:\n{tail}", None

        if skip_stemming:
            wav_path_out = os.path.join(output_dir, f"{out_name} (Converted Vocal).wav")
            return True, f"Saved to {wav_path_out}{cut_note}", wav_path_out

        mp3_path_out = os.path.join(output_dir, out_name + ".mp3")

        if not make_mp4:
            return True, f"Saved to {mp3_path_out}{cut_note}", mp3_path_out

        self._run_on_main(
            lambda: self.status_var.set(f"Building MP4 for {os.path.basename(input_path)}...")
        )
        mp4_path_out = os.path.join(output_dir, out_name + ".mp4")
        ok, error = self._build_mp4(image_path, mp3_path_out, mp4_path_out)
        if not ok:
            log_error(input_path, f"MP3 done, but MP4 failed:\n{error}")
            return False, f"MP3 done, but MP4 failed:\n{error}", mp3_path_out

        # The MP4 is the deliverable in this mode - the MP3 was only a step
        # toward it. Keep it alongside the rest of this song's intermediates
        # if asked to, otherwise there's no reason to keep it around.
        if keep_intermediates:
            song_intermediates_dir = os.path.join(output_dir, "intermediates", out_name)
            try:
                os.makedirs(song_intermediates_dir, exist_ok=True)
                shutil.move(mp3_path_out, os.path.join(song_intermediates_dir, os.path.basename(mp3_path_out)))
            except OSError as e:
                print(e)
        else:
            try:
                os.remove(mp3_path_out)
            except OSError as e:
                print(e)

        return True, f"Saved to {mp4_path_out}{cut_note}", mp4_path_out

    def _build_mp4(self, image_path, mp3_path, mp4_path):
        # -shortest alone doesn't reliably cap the output at the audio's
        # length when the audio is being re-encoded (-c:a aac) rather than
        # stream-copied - with a looping (infinite) image input, that
        # combination can run several seconds past where the audio actually
        # ends. Measuring the real duration and capping with -t is exact.
        duration = self._probe_duration(mp3_path)
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", image_path,
            "-i", mp3_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
        cmd += ["-t", f"{duration:.3f}"] if duration else ["-shortest"]
        cmd.append(mp4_path)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stdout.strip().splitlines()[-15:])
            return False, tail
        return True, None

    def _probe_duration(self, path):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            return float(result.stdout.strip())
        except (OSError, ValueError):
            return None

    def _finish_queue(self):
        self.progress.stop()
        errors = sum(1 for it in self.queue if it["status"] == "error")
        if errors:
            self.status_var.set(f"Queue finished - {errors} failed (see queue).")
        else:
            self.status_var.set("Queue finished.")
        self._update_go_button_state()

    def open_output_location(self):
        path = self.last_output_path
        if not path or not os.path.exists(path):
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif shutil.which("nautilus"):
                subprocess.Popen(["nautilus", "--select", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            print(e)


def parse_time(text):
    """'1:42', '1:42.5', '102' or '102.5' -> seconds (float), or None if unparseable."""
    text = text.strip()
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            minutes, seconds = int(minutes), float(seconds)
            if not (0 <= seconds < 60):
                return None
            return minutes * 60 + seconds
        return float(text)
    except ValueError:
        return None


def format_time(seconds):
    hundredths = int(round(seconds * 100))
    minutes, rest = divmod(hundredths, 6000)
    secs = f"{rest // 100:02d}.{rest % 100:02d}".rstrip("0").rstrip(".")
    return f"{minutes}:{secs}"


class MultiVoiceDialog(tk.Toplevel):
    """Per-song editor for multi-voice sections: rows of (start time, voice,
    pitch). The first row always starts at 0:00. Saving with fewer than two
    rows turns multi-voice off for the song."""

    def __init__(self, app, item):
        super().__init__(app)
        self.app = app
        self.item = item
        p = app.palette
        self.title("Multi-voice")
        self.configure(bg=p["bg"])
        self.transient(app)
        self.resizable(False, False)

        body = ttk.Frame(self, style="Hankify.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=10)

        duration = app._probe_duration(item["path"])
        length = f"  ({format_time(duration)})" if duration else ""
        ttk.Label(body, text=os.path.basename(item["path"]) + length,
                  style="Hankify.TLabel", wraplength=420).pack(anchor="w")
        ttk.Label(
            body, style="Hankify.Dim.TLabel", wraplength=420, justify="left",
            text="Each row starts a new voice at that time (m:ss or m:ss.s). Cuts are nudged up "
                 "to 0.5s to the nearest gap between words, so be as exact as you can.",
        ).pack(anchor="w", pady=(2, 8))

        self.rows_frame = ttk.Frame(body, style="Hankify.TFrame")
        self.rows_frame.pack(fill="x")
        header = ttk.Frame(self.rows_frame, style="Hankify.TFrame")
        header.pack(fill="x")
        for text, width in (("Start", 8), ("Voice", 34), ("Pitch", 5)):
            ttk.Label(header, text=text, width=width, style="Hankify.Dim.TLabel").pack(side="left", padx=(0, 4))

        self.rows = []
        existing = item.get("sections") or [
            {"start": 0, "voice": app.voice_model, "pitch": app.pitch_var.get()},
            {"start": None, "voice": app.voice_model, "pitch": app.pitch_var.get()},
        ]
        for s in existing:
            self._add_row(s["start"], s["voice"], s["pitch"])

        ttk.Button(body, text="+ Add section", style="Hankify.TButton",
                   command=lambda: self._add_row(None, self.rows[-1]["voice"].get(),
                                                 int(self.rows[-1]["pitch"].get()))
                   ).pack(anchor="w", pady=(6, 0))

        self.error_var = tk.StringVar()
        ttk.Label(body, textvariable=self.error_var, style="Hankify.TLabel",
                  foreground="#e74c3c", wraplength=420).pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(body, style="Hankify.TFrame")
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Save", style="Hankify.Accent.TButton",
                   command=self._save).pack(side="right")
        ttk.Button(buttons, text="Cancel", style="Hankify.TButton",
                   command=self.destroy).pack(side="right", padx=(0, 6))
        if item.get("sections"):
            ttk.Button(buttons, text="Single voice", style="Hankify.TButton",
                       command=self._clear).pack(side="left")

        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_visibility()
        self.grab_set()

    def _add_row(self, start, voice, pitch):
        row = ttk.Frame(self.rows_frame, style="Hankify.TFrame")
        row.pack(fill="x", pady=1)
        start_var = tk.StringVar(value="0:00" if not self.rows else
                                 (format_time(start) if start is not None else ""))
        entry = ttk.Entry(row, textvariable=start_var, width=8, style="Hankify.TEntry")
        entry.pack(side="left", padx=(0, 4))
        if not self.rows:
            entry.configure(state="disabled")
        voice_var = tk.StringVar(value=voice)
        ttk.Combobox(row, textvariable=voice_var, values=self.app.voice_models, width=32,
                     state="readonly", style="Hankify.TCombobox").pack(side="left", padx=(0, 4))
        pitch_var = tk.StringVar(value=str(pitch))
        ttk.Combobox(row, textvariable=pitch_var, values=[str(n) for n in range(-12, 13)], width=4,
                     state="readonly", style="Hankify.TCombobox").pack(side="left", padx=(0, 4))
        record = {"frame": row, "start": start_var, "voice": voice_var, "pitch": pitch_var}
        if self.rows:
            ttk.Button(row, text="✕", width=2, style="Hankify.TButton",
                       command=lambda: self._remove_row(record)).pack(side="left")
            if start is None:
                entry.focus_set()
        self.rows.append(record)

    def _remove_row(self, record):
        record["frame"].destroy()
        self.rows.remove(record)

    def _save(self):
        sections = []
        for n, r in enumerate(self.rows):
            start = 0.0 if n == 0 else parse_time(r["start"].get())
            if start is None:
                self.error_var.set(f'Row {n + 1}: "{r["start"].get()}" isn\'t a time - use m:ss, e.g. 1:42.')
                return
            if sections and start <= sections[-1]["start"]:
                self.error_var.set(f"Row {n + 1}: start times must go up row by row.")
                return
            sections.append({"start": start, "voice": r["voice"].get(), "pitch": int(r["pitch"].get())})
        if len(sections) < 2:
            self._clear()
            return
        if len({(s["voice"], s["pitch"]) for s in sections}) < 2:
            self.error_var.set("Every section uses the same voice - pick a different one somewhere.")
            return
        self.app._set_item_sections(self.item, sections)
        self.destroy()

    def _clear(self):
        self.app._set_item_sections(self.item, None)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
