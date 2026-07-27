#!/usr/bin/env python3
"""MLX Audio Separator — Tkinter front-end for the mlx-audio-separator CLI.

Runs on the dedicated venv Python (python.org 3.13) that ships Tcl/Tk. The GUI
is a thin driver over the CLI; all separation happens in the venv.
"""

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext

VENV_BIN = Path.home() / ".venvs" / "mlx-audio-separator" / "bin"
CLI = VENV_BIN / "mlx-audio-separator"
RUNNER = Path(__file__).resolve().parent / "runner.py"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "MLX Audio Separator"
MODEL_DIR = APP_SUPPORT / "models"
INDEX_CACHE = APP_SUPPORT / "models_index.json"
SETTINGS_FILE = APP_SUPPORT / "settings.json"
GITHUB_URL = "https://github.com/fdebkowski/mlx-audio-separator-gui"
APP_VERSION = "1.2.1"  # single source of truth; build.sh / build_bundle.sh read this
# Auto-update checks the repo's "latest release" and, in the packaged .app,
# downloads the new bundle and swaps it in place. Derived from GITHUB_URL so the
# repo lives in one spot.
_REPO = GITHUB_URL.split("github.com/", 1)[-1].strip("/")
RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
UPDATE_UA = f"MLX-Audio-Separator/{APP_VERSION} (macOS auto-update)"
UPDATE_CHECK_INTERVAL = 24 * 3600  # auto-check at most once a day

DEFAULT_MODEL = "mel_band_roformer_instrumental_instv8_gabox.ckpt"
# Bump when the curated extra_models.json changes so existing installs drop
# their cached model index and pick up the additions.
MODELS_REV = 3
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aiff", ".aif", ".ogg", ".opus", ".wma", ".mp4"}
FORMATS = ["FLAC", "WAV", "MP3"]
MAX_LOG_LINES = 5000  # cap the log widget so long/batch runs don't grow memory without bound
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
# Scraped from the engine's stdout. The CLI logs per-file errors but still
# exits 0 with an empty output list, so exit code alone can't be trusted.
MEM_ERROR_MARKERS = ("[metal::malloc]", "maximum allowed buffer size")
PROCESS_FAIL_MARKER = "Failed to process file"
# On a GPU-memory blow-up, retry once splitting the audio into chunks this many
# seconds long — the Metal buffer size scales with chunk length.
RETRY_CHUNK_SECONDS = 30

# --- Palette ported from the landing page (docs/tokens.css) ---
# Dark violet canvas + violet→magenta accent; the OKLCH tokens converted to hex
# so Tk (which has no OKLCH) can consume them.
PAPER, PAPER2, PAPER3 = "#0A080F", "#130F1A", "#1F1A27"
INK, INK2 = "#F3F0F9", "#C8C5CF"
RULE, RULE2 = "#322E3A", "#25222C"
MUTED, NEUTRAL = "#A39FAA", "#6D6A74"
ACCENT, ACCENT2 = "#AC77FA", "#ED62C1"
ACCENT_STRONG, ACCENT_HOVER = "#8445D1", "#7233BB"
ACCENT_INK, FOCUS = "#FEFAFE", "#C282FF"

# In the packaged .app the engine is embedded and sys.executable is the bundle
# itself; from source it lives in the venv and we drive it through runner.py.
FROZEN = getattr(sys, "frozen", False)


def engine_cmd(args):
    """Command that runs the separation engine with the given CLI args."""
    if FROZEN:
        return [sys.executable, "--separator-runner", *args]
    return [sys.executable, str(RUNNER), *args]


def tkdnd_dir():
    """Directory of the vendored tkdnd package (holds pkgIndex.tcl), or None.

    Bundled so users can drag audio onto the window with nothing to install.
    In the frozen .app the folder is added as data (PyInstaller extracts it to
    sys._MEIPASS); from source it lives under vendor/ next to this file.
    """
    candidates = []
    if FROZEN:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "vendor" / "tkdnd")
        candidates.append(
            Path(sys.executable).resolve().parent.parent / "Resources" / "vendor" / "tkdnd")
    candidates.append(Path(__file__).resolve().parent / "vendor" / "tkdnd")
    for c in candidates:
        if (c / "pkgIndex.tcl").exists():
            return c
    return None


def stem_count(model):
    """How many stems a model outputs (2 for the vocals/instrumental default)."""
    return len(model.get("stems") or []) or 2


def stem_options(model_stems):
    """Map a model's stems to selector labels -> --single_stem value.

    Most models declare no stems in metadata but are 2-stem vocals/instrumental
    separators, so fall back to that pair to always offer 'Vocals/Instrumental only'.
    """
    stems = [s.lower() for s in (model_stems or [])] or ["vocals", "instrumental"]
    opts = {"All stems": None}
    for s in stems:
        opts[f"{s.capitalize()} only"] = s
    return opts


def best_sdr(info):
    scores = info.get("scores") or {}
    target = info.get("target_stem")
    vals = []
    for stem, s in scores.items():
        if isinstance(s, dict) and isinstance(s.get("SDR"), (int, float)):
            vals.append((stem, s["SDR"]))
    if not vals:
        return None
    if target:
        for stem, v in vals:
            if stem == target:
                return v
    return max(v for _, v in vals)


def sdr_num(m):
    return m["sdr"] if isinstance(m["sdr"], (int, float)) else float("-inf")


class _UpdateCancelled(Exception):
    """Raised inside the download worker when the user cancels an update."""


def parse_version(s):
    """'v1.12.0' / '1.12' -> (1, 12, 0…). Ignores any pre-release suffix."""
    m = re.match(r"\d+(?:\.\d+)*", (s or "").strip().lstrip("vV"))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def is_newer_version(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _ssl_context():
    """TLS context that actually verifies certs.

    The python.org interpreter (and the frozen bundle) ship OpenSSL without a
    system trust store, so stdlib ssl can't verify certs unless certifi's CA
    bundle is used. certifi is already present in the venv and embedded in the
    .app (the engine downloads models over TLS), so use it when importable and
    fall back to the default context otherwise.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_latest_release():
    """Query the repo's latest GitHub release, returning the fields we need."""
    req = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": UPDATE_UA,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as r:
        data = json.loads(r.read().decode("utf-8"))
    assets = data.get("assets") or []
    asset = next((a for a in assets if (a.get("name") or "").endswith("-macos-arm64.zip")), None)
    if asset is None:
        asset = next((a for a in assets if (a.get("name") or "").endswith(".zip")), None)
    tag = data.get("tag_name") or ""
    return {
        "version": tag.lstrip("vV"),
        "html_url": data.get("html_url") or (GITHUB_URL + "/releases/latest"),
        "asset_url": (asset or {}).get("browser_download_url"),
        "asset_name": (asset or {}).get("name"),
        "size": (asset or {}).get("size") or 0,
        "digest": (asset or {}).get("digest"),
    }


# Detached helper: waits for the running app to quit, swaps its bundle for the
# freshly downloaded one, then relaunches. Args: <pid> <old_app> <new_app>.
SWAP_SCRIPT = r"""#!/bin/sh
PID="$1"; OLD="$2"; NEW="$3"
i=0
while kill -0 "$PID" 2>/dev/null; do
  i=$((i+1)); [ "$i" -ge 150 ] && break; sleep 0.2
done
BAK="${OLD}.old-$$"
if ! /bin/mv "$OLD" "$BAK" 2>/dev/null; then
  /usr/bin/open -R "$NEW"; exit 1
fi
if /bin/mv "$NEW" "$OLD" 2>/dev/null; then
  /bin/rm -rf "$BAK"
  /usr/bin/xattr -dr com.apple.quarantine "$OLD" 2>/dev/null
  /usr/bin/open "$OLD"; exit 0
fi
# Swap failed — roll the original back into place.
/bin/rm -rf "$OLD" 2>/dev/null
/bin/mv "$BAK" "$OLD" 2>/dev/null
/usr/bin/open "$OLD"; exit 1
"""


class SeparatorApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.models = []           # list of dicts: friendly, filename, arch, stems, sdr
        self.filtered = []
        self.model_by_file = {}
        self.downloaded = set()    # filenames of model weights present on disk
        self.stem_map = {"All stems": None}
        # Full paths; the listbox shows friendly names. Seed from argv (Open With…).
        self.input_paths = [p for p in sys.argv[1:] if os.path.exists(p)]
        self._cancelled = False
        self._determinate = False  # progress bar switched to determinate mode
        self._saw_memory_error = False
        self._saw_process_failure = False
        self._chunked_retry = False    # current run is an auto-retry with chunking
        self._base_cmd = None          # command of the in-flight run, for retrying
        self._update_checking = False
        self._update_cancel = False
        self._update_downloading = False
        self._pending_update = None
        self.update_msg = None
        self.update_progress = None
        # None = the recommended default order from the worker; otherwise a
        # (column, reverse) pair driven by clicking the model table headers.
        self._sort = None

        root.title("MLX Audio Separator")

        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = self._load_settings()

        self._build_ui()

        # The dark-violet theme uses taller rows and larger fonts, so let the
        # window size itself to the built content instead of a fixed guess —
        # otherwise the bottom action bar is clipped at launch. Clamp to the
        # screen so it still fits shorter laptop displays.
        root.update_idletasks()
        content_h = root.winfo_reqheight()
        # Reserve ~72px for the macOS menu bar and window title bar; keeps the
        # full UI on 1440x900 laptops while still fitting shorter displays.
        win_h = min(content_h, root.winfo_screenheight() - 72)
        root.geometry(f"920x{win_h}")
        root.minsize(820, win_h)
        self._check_cli()
        self._load_models_async()
        self.root.after(80, self._poll_queue)
        self.root.after(1500, lambda: self._check_updates(manual=False))

    # ---------- UI ----------
    def _setup_theme(self):
        """Port the landing-page look (dark violet canvas, violet→magenta accent)
        onto Tk. macOS 'aqua' can't recolour its native buttons/fields, so drive
        everything through the fully themeable 'clam' engine instead."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base = tkfont.nametofont("TkDefaultFont")
        self.base_size = base.cget("size") or 13
        avail = set(tkfont.families(self.root))
        pick = lambda names, fb: next((n for n in names if n in avail), fb)
        self.body_family = pick(["Geist", "SF Pro Text"], base.cget("family"))
        self.display_family = pick(["Space Grotesk", "Geist"], self.body_family)
        self.mono_family = pick(["Geist Mono", "SF Mono", "JetBrains Mono"], "Menlo")
        rowh = self.base_size + 14

        self.root.configure(bg=PAPER)
        self.root.option_add("*TCombobox*Listbox.background", PAPER2)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_STRONG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_INK)

        style.configure(".", background=PAPER, foreground=INK,
                        fieldbackground=PAPER2, bordercolor=RULE,
                        lightcolor=PAPER, darkcolor=PAPER,
                        insertcolor=INK, focuscolor=FOCUS,
                        font=(self.body_family, self.base_size))
        style.configure("TFrame", background=PAPER)
        style.configure("TLabel", background=PAPER, foreground=INK)
        style.configure("TSeparator", background=RULE)

        style.configure("TButton", background=PAPER3, foreground=INK,
                        bordercolor=RULE, relief="flat", padding=(12, 6))
        style.map("TButton",
                  background=[("pressed", RULE), ("active", RULE2),
                              ("disabled", PAPER2)],
                  foreground=[("disabled", NEUTRAL)],
                  bordercolor=[("focus", ACCENT)])

        # The primary action (Separate) carries the accent fill from the LP CTA.
        style.configure("Accent.TButton", background=ACCENT_STRONG,
                        foreground=ACCENT_INK, bordercolor=ACCENT_STRONG,
                        padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_HOVER), ("active", ACCENT_HOVER),
                              ("disabled", PAPER3)],
                  foreground=[("disabled", NEUTRAL)],
                  bordercolor=[("disabled", RULE)])

        for w in ("TEntry", "TCombobox"):
            style.configure(w, fieldbackground=PAPER2, foreground=INK,
                            bordercolor=RULE, lightcolor=RULE, darkcolor=RULE,
                            arrowcolor=MUTED, padding=4)
            style.map(w, bordercolor=[("focus", ACCENT)],
                      lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        style.map("TCombobox",
                  fieldbackground=[("readonly", PAPER2)],
                  foreground=[("readonly", INK)],
                  arrowcolor=[("active", INK)])

        for w in ("TCheckbutton", "TRadiobutton"):
            style.configure(w, background=PAPER, foreground=INK,
                            indicatorbackground=PAPER2, indicatorforeground=ACCENT)
            style.map(w, background=[("active", PAPER)],
                      indicatorbackground=[("pressed", PAPER3), ("active", PAPER2)],
                      foreground=[("disabled", NEUTRAL)])

        style.configure("Treeview", background=PAPER2, fieldbackground=PAPER2,
                        foreground=INK, bordercolor=RULE, borderwidth=0,
                        rowheight=rowh)
        style.map("Treeview", background=[("selected", ACCENT_STRONG)],
                  foreground=[("selected", ACCENT_INK)])
        style.configure("Treeview.Heading", background=PAPER3, foreground=MUTED,
                        bordercolor=RULE, relief="flat", padding=(8, 5),
                        font=(self.body_family, max(self.base_size - 1, 10), "bold"))
        style.map("Treeview.Heading", background=[("active", RULE2)],
                  foreground=[("active", INK)])

        for w in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(w, background=PAPER3, troughcolor=PAPER,
                            bordercolor=PAPER, arrowcolor=MUTED, relief="flat")
            style.map(w, background=[("active", RULE)])

        style.configure("TProgressbar", background=ACCENT, troughcolor=PAPER2,
                        bordercolor=RULE, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_ui(self):
        self._setup_theme()
        self.sub_color = MUTED
        self.accent = ACCENT
        self.h_font = tkfont.Font(family=self.display_family,
                                  size=self.base_size + 3, weight="bold")

        self._build_menubar()

        # Update banner sits above everything; stays empty until an update shows.
        self.banner_holder = ttk.Frame(self.root)
        self.banner_holder.pack(side="top", fill="x")

        main = ttk.Frame(self.root, padding=(16, 6, 16, 10))
        main.pack(fill="both", expand=True)

        # -- Step 1: music in
        self._section(main, "1", "Add your music",
                      "The songs you want to split into separate stems.")
        in_frame = ttk.Frame(main)
        in_frame.pack(fill="x", pady=(4, 0))
        list_wrap = ttk.Frame(in_frame)
        list_wrap.pack(side="left", fill="both", expand=True)
        self.files_list = tk.Listbox(
            list_wrap, height=4, activestyle="none",
            bg=PAPER2, fg=INK, selectbackground=ACCENT_STRONG,
            selectforeground=ACCENT_INK, highlightthickness=1,
            highlightbackground=RULE, highlightcolor=ACCENT, bd=0,
            font=(self.body_family, self.base_size))
        files_vs = ttk.Scrollbar(list_wrap, orient="vertical", command=self.files_list.yview)
        self.files_list.configure(yscrollcommand=files_vs.set)
        self.files_list.pack(side="left", fill="both", expand=True)
        files_vs.pack(side="right", fill="y")
        self.files_hint = tk.Label(
            self.files_list, text="No music yet.\nClick here to choose audio files…",
            justify="center", fg=self.sub_color, bg=self.files_list.cget("background"),
            bd=0, cursor="pointinghand")
        self.files_hint.bind("<Button-1>", lambda *_: self._add_files())
        self._dnd_hint = "No music yet.\nDrag audio in, or click to choose files…"
        btns = ttk.Frame(in_frame)
        btns.pack(side="right", fill="y", padx=(8, 0))
        ttk.Button(btns, text="Add Files…", command=self._add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Add Folder…", command=self._add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear", command=self._clear_files).pack(fill="x", pady=2)

        # -- Step 2: model
        self._section(main, "2", "Pick a model",
                      "Each model is trained for a different job — vocals, instrumentals, "
                      "drums… Higher quality (SDR) usually means a cleaner split.")
        model_frame = ttk.Frame(main)
        model_frame.pack(fill="both", expand=True, pady=(4, 0))
        top = ttk.Frame(model_frame)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(top, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=6)
        self.refresh_btn = ttk.Button(top, text="Refresh list",
                                      command=lambda: self._load_models_async(force=True))
        self.refresh_btn.pack(side="right")
        self.downloaded_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Downloaded only", variable=self.downloaded_only_var,
                        command=self._toggle_downloaded_only).pack(side="right", padx=(0, 10))

        cols = ("name", "stems", "sdr", "disk")
        tree_wrap = ttk.Frame(model_frame)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=8,
                                 selectmode="browse")
        self._heading_text = {"name": "Model", "stems": "Stems",
                              "sdr": "Quality (SDR)", "disk": "On disk"}
        for c, text in self._heading_text.items():
            self.tree.heading(c, text=text, command=lambda col=c: self._sort_by(col))
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("stems", width=180, anchor="w")
        self.tree.column("sdr", width=95, anchor="center")
        self.tree.column("disk", width=60, anchor="center")
        vs = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_model_select)
        self.tree.bind("<Button-2>", self._show_model_menu)
        self.tree.bind("<Button-3>", self._show_model_menu)
        self.tree.bind("<Control-Button-1>", self._show_model_menu)
        ttk.Label(model_frame,
                  text="Models download automatically the first time you use them "
                       "(right-click a downloaded one to manage it). Click a column to "
                       "sort — e.g. Stems to find the models that split into the most tracks.",
                  foreground=self.sub_color, wraplength=780, justify="left").pack(
                      anchor="w", pady=(4, 0))

        # -- Step 3: output
        self._section(main, "3", "Choose what you get", None)
        opt = ttk.Frame(main)
        opt.pack(fill="x", pady=(4, 0))
        row1 = ttk.Frame(opt)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Format:").pack(side="left")
        fmt = self.settings.get("format")
        self.format_var = tk.StringVar(value=fmt if fmt in FORMATS else "FLAC")
        self.format_var.trace_add("write", lambda *_: self._save_format())
        for f in FORMATS:
            ttk.Radiobutton(row1, text=f, value=f, variable=self.format_var).pack(side="left", padx=4)
        ttk.Label(row1, text="   Stems:").pack(side="left", padx=(16, 2))
        self.stem_var = tk.StringVar(value="All stems")
        self.stem_combo = ttk.Combobox(row1, textvariable=self.stem_var, state="readonly",
                                       width=18, values=["All stems"])
        self.stem_combo.pack(side="left", padx=4)
        row2 = ttk.Frame(opt)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Save to:").pack(side="left")
        self.outdir_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        self.outdir_var.trace_add("write", lambda *_: self._save_outdir())
        ttk.Entry(row2, textvariable=self.outdir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row2, text="Choose…", command=self._choose_outdir).pack(side="left")
        ttk.Label(opt, text="Leave empty to save the stems next to each song.",
                  foreground=self.sub_color).pack(anchor="w", pady=(0, 2))

        # -- Action bar
        ttk.Separator(main).pack(fill="x", pady=(10, 8))
        run = ttk.Frame(main)
        run.pack(fill="x")
        self.run_btn = ttk.Button(run, text="Separate", default="active",
                                  style="Accent.TButton",
                                  command=self._start, state="disabled")
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(run, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.open_btn = ttk.Button(run, text="Open Output Folder", command=self._open_output,
                                   state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        self.details_btn = ttk.Button(run, text="Show Details", command=self._toggle_details)
        self.details_btn.pack(side="right")
        self.progress = ttk.Progressbar(run, mode="indeterminate", length=200)
        self.progress.pack(side="right", padx=(0, 10))

        self.status_var = tk.StringVar(value="Loading models…")
        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(6, 0))

        # -- Log (collapsed by default; opens automatically on errors)
        self.details_visible = False
        self.log_frame = ttk.Frame(main)
        self.log = scrolledtext.ScrolledText(
            self.log_frame, height=8, wrap="word", state="disabled",
            font=(self.mono_family, 11), bg=PAPER2, fg=INK2,
            insertbackground=INK, selectbackground=ACCENT_STRONG,
            selectforeground=ACCENT_INK, highlightthickness=1,
            highlightbackground=RULE, bd=0, padx=8, pady=6)
        self.log.pack(fill="both", expand=True)

        self._refresh_inputs_ui()
        self._setup_dnd()

    def _section(self, parent, num, title, sub):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text=num, font=self.h_font, foreground=self.accent).pack(side="left")
        ttk.Label(row, text=title, font=self.h_font).pack(side="left", padx=(7, 0))
        if sub:
            ttk.Label(parent, text=sub, foreground=self.sub_color, wraplength=780,
                      justify="left").pack(fill="x", pady=(0, 2))

    def _build_menubar(self):
        menubar = tk.Menu(self.root)
        filem = tk.Menu(menubar, tearoff=0)
        filem.add_command(label="Add Files…", accelerator="Cmd+O", command=self._add_files)
        filem.add_command(label="Add Folder…", accelerator="Shift+Cmd+O", command=self._add_folder)
        filem.add_separator()
        filem.add_command(label="Open Output Folder", command=self._open_output)
        filem.add_command(label="Open Models Folder",
                          command=lambda: subprocess.run(["open", str(MODEL_DIR)]))
        menubar.add_cascade(label="File", menu=filem)
        helpm = tk.Menu(menubar, tearoff=0, name="help")
        helpm.add_command(label=f"Version {APP_VERSION}", state="disabled")
        helpm.add_separator()
        helpm.add_command(label="Check for Updates…",
                          command=lambda: self._check_updates(manual=True))
        self.auto_update_var = tk.BooleanVar(value=self.settings.get("auto_update_check", True))
        helpm.add_checkbutton(label="Automatically check for updates",
                              variable=self.auto_update_var,
                              command=self._toggle_auto_update)
        helpm.add_separator()
        helpm.add_command(label="MLX Audio Separator on GitHub",
                          command=lambda: webbrowser.open(GITHUB_URL))
        helpm.add_command(label="Report an Issue",
                          command=lambda: webbrowser.open(GITHUB_URL + "/issues"))
        menubar.add_cascade(label="Help", menu=helpm)
        self.root.configure(menu=menubar)
        self.root.bind("<Command-o>", lambda *_: self._add_files())
        self.root.bind("<Command-O>", lambda *_: self._add_folder())

    def _toggle_details(self):
        if self.details_visible:
            self.log_frame.pack_forget()
            self.details_btn.configure(text="Show Details")
        else:
            self.log_frame.pack(fill="both", expand=True, pady=(8, 0))
            self.details_btn.configure(text="Hide Details")
        self.details_visible = not self.details_visible

    # ---------- CLI check ----------
    def _check_cli(self):
        if FROZEN:
            return  # engine is embedded in the bundle
        if not CLI.exists():
            self._log(f"ERROR: Separation engine not found at {CLI}\n\n"
                      "Run setup.sh from the project folder, or install it manually:\n"
                      "  python3 -m venv ~/.venvs/mlx-audio-separator\n"
                      "  ~/.venvs/mlx-audio-separator/bin/pip install 'mlx-audio-separator[convert]'\n")
            if not self.details_visible:
                self._toggle_details()
            messagebox.showerror("Separation engine missing",
                                 f"Could not find the separator CLI at:\n{CLI}\n\n"
                                 "Run setup.sh from the project folder to install it. "
                                 "See the details panel for the manual install commands.")

    # ---------- Inputs ----------
    def _display_name(self, p):
        path = Path(p)
        return f"📁 {path.name}" if path.is_dir() else path.name

    def _refresh_inputs_ui(self):
        self.files_list.delete(0, "end")
        for p in self.input_paths:
            self.files_list.insert("end", f" {self._display_name(p)}")
        if self.input_paths:
            self.files_hint.place_forget()
        else:
            self.files_hint.place(relx=0.5, rely=0.5, anchor="center")
        self._update_run_state()

    def _add_files(self):
        audio_pat = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        paths = filedialog.askopenfilenames(
            title="Choose audio files",
            filetypes=[("Audio files", audio_pat), ("All files", "*")])
        for p in paths:
            if p not in self.input_paths:
                self.input_paths.append(p)
        self._refresh_inputs_ui()

    def _add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of audio")
        if d and d not in self.input_paths:
            self.input_paths.append(d)
        self._refresh_inputs_ui()

    def _remove_selected(self):
        for i in reversed(self.files_list.curselection()):
            del self.input_paths[i]
        self._refresh_inputs_ui()

    def _clear_files(self):
        self.input_paths = []
        self._refresh_inputs_ui()

    def _choose_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir_var.set(d)

    # ---------- Drag & drop ----------
    def _setup_dnd(self):
        """Let users drag audio files/folders from Finder onto the file list.

        Uses the vendored tkdnd Tcl extension; if it can't load (e.g. an
        unbundled dev checkout on another arch) the app just runs without it.
        """
        d = tkdnd_dir()
        if d is None:
            return
        try:
            self.root.tk.call("lappend", "auto_path", str(d))
            self.root.tk.call("package", "require", "tkdnd")
        except tk.TclError as e:
            self._log(f"Drag-and-drop unavailable: {e}\n")
            return
        on_drop = self.files_list.register(self._on_drop)
        on_enter = self.files_list.register(lambda *_: self._dnd_highlight(True) or "copy")
        on_leave = self.files_list.register(lambda *_: self._dnd_highlight(False) or "copy")
        for w in (self.files_list, self.files_hint):
            try:
                self.root.tk.call("tkdnd::drop_target", "register", w._w, "DND_Files")
                self.root.tk.call("bind", w._w, "<<Drop>>", f"{on_drop} %D")
                self.root.tk.call("bind", w._w, "<<DropEnter>>", f"{on_enter}")
                self.root.tk.call("bind", w._w, "<<DropLeave>>", f"{on_leave}")
            except tk.TclError:
                pass
        self.files_hint.configure(text=self._dnd_hint)

    def _dnd_highlight(self, on):
        # Thicken to the accent while a drag hovers; otherwise restore the
        # themed 1px rule border the listbox was created with.
        try:
            if on:
                self.files_list.configure(highlightthickness=2,
                                          highlightbackground=self.accent)
            else:
                self.files_list.configure(highlightthickness=1,
                                          highlightbackground=RULE)
        except (tk.TclError, NameError):
            pass

    def _on_drop(self, data):
        added = 0
        for p in self.root.tk.splitlist(data):
            if p in self.input_paths:
                continue
            if os.path.isdir(p) or Path(p).suffix.lower() in AUDIO_EXTS:
                self.input_paths.append(p)
                added += 1
        self._dnd_highlight(False)
        if added:
            self._refresh_inputs_ui()
        return "copy"

    # ---------- Settings ----------
    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        except Exception:
            pass

    def _save_outdir(self):
        self.settings["output_dir"] = self.outdir_var.get().strip()
        self._save_settings()

    def _save_format(self):
        self.settings["format"] = self.format_var.get()
        self._save_settings()

    # ---------- Updates ----------
    def _toggle_auto_update(self):
        self.settings["auto_update_check"] = bool(self.auto_update_var.get())
        self._save_settings()

    def _check_updates(self, manual):
        if self._update_checking:
            if manual:
                messagebox.showinfo("Check for Updates", "Already checking for updates…")
            return
        if not manual:
            if not self.settings.get("auto_update_check", True):
                return
            last = float(self.settings.get("last_update_check", 0) or 0)
            if time.time() - last < UPDATE_CHECK_INTERVAL:
                return
        self._update_checking = True
        if manual:
            self.status_var.set("Checking for updates…")
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual):
        res = {"manual": manual}
        try:
            info = fetch_latest_release()
            res["ok"] = True
            res["info"] = info
            res["newer"] = bool(info["version"]) and is_newer_version(info["version"], APP_VERSION)
        except Exception as e:
            res["ok"] = False
            res["error"] = str(e)
        self.q.put(("update_result", res))

    def _start_update(self):
        info = self._pending_update
        if not info:
            return
        # From source, or if a release has no downloadable asset, just open the page.
        if not (FROZEN and info.get("asset_url")):
            webbrowser.open(info["html_url"])
            return
        if self.proc is not None:
            messagebox.showinfo("Please wait",
                                "Finish or cancel the current separation before updating.")
            return
        size_mb = (info.get("size") or 0) / 1e6
        detail = f" (~{size_mb:.0f} MB)" if size_mb else ""
        if not messagebox.askyesno(
                "Install update",
                f"Download version {info['version']}{detail} and install it now?\n\n"
                "The app will replace itself and relaunch."):
            return
        self._update_cancel = False
        self._update_downloading = True
        self._render_update_downloading()
        threading.Thread(target=self._update_download_worker, args=(info,), daemon=True).start()

    def _cancel_update(self):
        self._update_cancel = True

    def _update_download_worker(self, info):
        staging = APP_SUPPORT / "update-staging"
        try:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            zip_path = staging / (info.get("asset_name") or "update.zip")
            req = urllib.request.Request(info["asset_url"], headers={"User-Agent": UPDATE_UA})
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r, \
                    open(zip_path, "wb") as f:
                total = int(r.headers.get("Content-Length") or info.get("size") or 0)
                done = last = 0
                while True:
                    if self._update_cancel:
                        raise _UpdateCancelled()
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if done - last >= 1_000_000:
                        last = done
                        self.q.put(("update_prog", (done, total)))
            self.q.put(("update_prog", (done, total)))
            digest = info.get("digest") or ""
            if digest.startswith("sha256:") and h.hexdigest() != digest.split(":", 1)[1]:
                raise RuntimeError("The downloaded update failed its integrity check.")
            unpack = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(staging)],
                                    capture_output=True, text=True)
            if unpack.returncode != 0:
                raise RuntimeError("Could not unpack the update. " + unpack.stderr.strip())
            apps = list(staging.glob("*.app")) or list(staging.rglob("*.app"))
            if not apps:
                raise RuntimeError("The update package did not contain an app.")
            new_app = apps[0]
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new_app)],
                           capture_output=True)
            self.q.put(("update_ready", str(new_app)))
        except _UpdateCancelled:
            shutil.rmtree(staging, ignore_errors=True)
            self.q.put(("update_cancelled", None))
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            self.q.put(("update_error", str(e)))

    def _install_update(self, new_app):
        new_app = Path(new_app)
        try:
            app_bundle = Path(sys.executable).resolve().parents[2]
        except Exception:
            app_bundle = None
        if (app_bundle is None or app_bundle.suffix != ".app"
                or not os.access(app_bundle.parent, os.W_OK)):
            subprocess.run(["open", "-R", str(new_app)])
            messagebox.showinfo(
                "Update downloaded",
                "The update was downloaded but couldn't be installed automatically "
                "(this app's folder isn't writable).\n\n"
                f"Drag “{new_app.name}” — now shown in Finder — into your Applications "
                "folder, replacing the old copy.")
            self._render_update_available()
            return
        script = APP_SUPPORT / "update-swap.sh"
        try:
            script.write_text(SWAP_SCRIPT)
            os.chmod(script, 0o755)
            subprocess.Popen(
                ["/bin/sh", str(script), str(os.getpid()), str(app_bundle), str(new_app)],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            messagebox.showerror("Update failed", f"Couldn't start the installer:\n{e}")
            self._render_update_available()
            return
        self.status_var.set("Installing update… the app will relaunch.")
        self.root.after(200, self._quit_now)

    def _quit_now(self):
        try:
            self.root.destroy()
        finally:
            os._exit(0)

    def _clear_banner(self):
        for w in self.banner_holder.winfo_children():
            w.destroy()
        self.update_msg = None
        self.update_progress = None

    def _hide_update_banner(self):
        self._clear_banner()

    def _render_update_available(self):
        info = self._pending_update
        if not info:
            return
        self._clear_banner()
        bar = ttk.Frame(self.banner_holder, padding=(16, 8))
        bar.pack(fill="x")
        ttk.Separator(self.banner_holder).pack(fill="x")
        ttk.Label(bar, text="🔄").pack(side="left")
        ttk.Label(bar, text=f"Version {info['version']} is available "
                            f"(you have {APP_VERSION}).").pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Later", command=self._hide_update_banner).pack(side="right")
        ttk.Button(bar, text="Release Notes",
                   command=lambda: webbrowser.open(info["html_url"])).pack(side="right", padx=(0, 8))
        primary = "Update Now" if (FROZEN and info.get("asset_url")) else "Open Download Page"
        ttk.Button(bar, text=primary, command=self._start_update).pack(side="right", padx=(0, 8))

    def _render_update_downloading(self):
        self._clear_banner()
        bar = ttk.Frame(self.banner_holder, padding=(16, 8))
        bar.pack(fill="x")
        ttk.Separator(self.banner_holder).pack(fill="x")
        ttk.Label(bar, text="⬇️").pack(side="left")
        self.update_msg = ttk.Label(bar, text="Starting download…")
        self.update_msg.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Cancel", command=self._cancel_update).pack(side="right")
        self.update_progress = ttk.Progressbar(bar, mode="determinate", maximum=100, length=180)
        self.update_progress.pack(side="right", padx=(0, 8))

    # ---------- Models ----------
    def _load_models_async(self, force=False):
        self.refresh_btn.configure(state="disabled")
        self.status_var.set("Loading model list…")
        threading.Thread(target=self._load_models_worker, args=(force,),
                         daemon=True).start()

    def _load_models_worker(self, force=False):
        data = None
        # Prefer cache (unless stale or a refresh was requested), else fetch
        # from the CLI.
        if not force and INDEX_CACHE.exists():
            try:
                cached = json.loads(INDEX_CACHE.read_text())
                if isinstance(cached, dict) and cached.get("rev") == MODELS_REV:
                    data = cached["index"]
            except Exception:
                data = None
        if data is None and (FROZEN or CLI.exists()):
            try:
                out = subprocess.run(
                    engine_cmd(["--list_models", "--list_format", "json",
                                "--log_level", "error", "--model_file_dir", str(MODEL_DIR)]),
                    capture_output=True, text=True, timeout=120,
                )
                raw = out.stdout
                idxs = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
                if idxs:
                    data = json.loads(raw[min(idxs):])
                    INDEX_CACHE.write_text(json.dumps({"rev": MODELS_REV,
                                                       "index": data}))
            except Exception as e:
                self.q.put(("models_error", str(e)))
                return
        if data is None:
            self.q.put(("models_error", "No model list available."))
            return

        models = []
        for arch, group in data.items():
            if not isinstance(group, dict):
                continue
            for friendly, info in group.items():
                if not isinstance(info, dict):
                    continue
                fn = info.get("filename")
                if not fn:
                    continue
                sdr = best_sdr(info)
                models.append({
                    "friendly": friendly,
                    "filename": fn,
                    "arch": arch,
                    "stems": info.get("stems") or [],
                    "sdr": sdr,
                })
        # Recommended first, then best quality, so casual users see good picks on top.
        models.sort(key=lambda m: (m["filename"] != DEFAULT_MODEL, -sdr_num(m),
                                   m["friendly"].lower()))
        self.q.put(("models_loaded", models))

    def _populate_tree(self, models):
        prev_sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for m in models:
            sdr = f"{m['sdr']:.1f}" if isinstance(m["sdr"], (int, float)) else "—"
            names = ", ".join(m["stems"]) or "vocals, instrumental"
            stems = f"{stem_count(m)} · {names}"
            name = m["friendly"]
            if m["filename"] == DEFAULT_MODEL:
                name += "   ★ recommended"
            disk = "✓" if m["filename"] in self.downloaded else ""
            self.tree.insert("", "end", iid=m["filename"],
                             values=(name, stems, sdr, disk))
        if prev_sel and self.tree.exists(prev_sel[0]):
            self.tree.selection_set(prev_sel[0])
            self.tree.see(prev_sel[0])

    def _apply_filter(self):
        term = self.search_var.get().strip().lower()
        items = self.models
        if term:
            items = [
                m for m in items
                if term in m["friendly"].lower()
                or term in m["filename"].lower()
                or term in m["arch"].lower()
                or any(term in s.lower() for s in m["stems"])
            ]
        if self.downloaded_only_var.get():
            items = [m for m in items if m["filename"] in self.downloaded]
        self.filtered = self._sorted(items)
        self._populate_tree(self.filtered)

    def _sorted(self, items):
        """Order for the table: the recommended default, or a clicked column.

        Sorts are stable, so within a column's ties models keep the incoming
        recommended/SDR order (e.g. sort by Stems and the best model of each
        stem count floats to the top of its group)."""
        if self._sort is None:
            return list(items)
        col, reverse = self._sort
        keys = {
            "name": lambda m: m["friendly"].lower(),
            "stems": stem_count,
            "sdr": sdr_num,
            "disk": lambda m: m["filename"] in self.downloaded,
        }
        return sorted(items, key=keys.get(col, keys["name"]), reverse=reverse)

    def _sort_by(self, col):
        # First click on a column sorts most-useful-first: names A→Z, the
        # numeric/flag columns high→low (most stems, best SDR, downloaded first).
        if self._sort and self._sort[0] == col:
            reverse = not self._sort[1]
        else:
            reverse = col != "name"
        self._sort = (col, reverse)
        self._update_sort_headings()
        self._apply_filter()

    def _update_sort_headings(self):
        for c, text in self._heading_text.items():
            if self._sort and self._sort[0] == c:
                text += " ▼" if self._sort[1] else " ▲"
            self.tree.heading(c, text=text)

    def _on_model_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            self._update_run_state()
            return
        m = self.model_by_file.get(sel[0])
        if not m:
            return
        self.stem_map = stem_options(m.get("stems"))
        labels = list(self.stem_map.keys())
        self.stem_combo.configure(values=labels)
        if self.stem_var.get() not in labels:
            self.stem_var.set("All stems")
        self.settings["model"] = sel[0]
        self._save_settings()
        self._update_run_state()

    def _selected_model(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _update_run_state(self):
        ready = (self.proc is None and self.input_paths
                 and self._selected_model() is not None)
        self.run_btn.configure(state="normal" if ready else "disabled")

    # ---------- Downloaded models ----------
    def _scan_downloaded(self):
        """Filenames of model weight files present on disk (skip configs/aux)."""
        aux = {".yaml", ".yml", ".json", ".txt", ".md"}
        self.downloaded = {
            p.name for p in MODEL_DIR.glob("*")
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() not in aux
        }
        return self.downloaded

    def _toggle_downloaded_only(self):
        self._scan_downloaded()
        self._apply_filter()

    def _show_model_menu(self, evt):
        row = self.tree.identify_row(evt.y)
        if not row:
            return
        self.tree.selection_set(row)
        downloaded = row in self.downloaded
        state = "normal" if downloaded else "disabled"
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="Reveal in Finder", state=state,
                         command=lambda: self._reveal_model(row))
        menu.add_command(label="Delete download", state=state,
                         command=lambda: self._delete_model(row))
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    def _reveal_model(self, filename):
        p = MODEL_DIR / filename
        if p.exists():
            subprocess.run(["open", "-R", str(p)])

    def _delete_model(self, filename):
        p = MODEL_DIR / filename
        if not messagebox.askyesno(
                "Delete model",
                f"Delete this downloaded model file?\n\n{filename}\n\n"
                "It will be downloaded again the next time you use it."):
            return
        try:
            p.unlink()
        except OSError as e:
            messagebox.showerror("Delete failed", str(e))
            return
        self._scan_downloaded()
        self._apply_filter()

    # ---------- Run ----------
    def _gather_inputs(self):
        return list(self.input_paths)

    def _start(self):
        if self.proc is not None:
            return
        inputs = self._gather_inputs()
        if not inputs:
            messagebox.showwarning("No input", "Add at least one audio file or a folder.")
            return
        model = self._selected_model()
        if not model:
            messagebox.showwarning("No model", "Select a model from the list.")
            return

        outdir = self.outdir_var.get().strip()
        if not outdir:
            first = Path(inputs[0])
            outdir = str(first if first.is_dir() else first.parent)
        self._last_outdir = outdir

        cmd = engine_cmd(inputs + [
            "-m", model,
            "--output_format", self.format_var.get(),
            "--output_dir", outdir,
            "--model_file_dir", str(MODEL_DIR),
        ])
        stem = self.stem_map.get(self.stem_var.get())
        if stem:
            cmd += ["--single_stem", stem]

        self._base_cmd = list(cmd)
        self._chunked_retry = False
        self._clear_log()
        self._log("$ " + " ".join(self._quote(c) for c in cmd) + "\n\n")
        self._cancelled = False
        self._determinate = False
        self._saw_memory_error = False
        self._saw_process_failure = False
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        if model not in self.downloaded:
            self.status_var.set("Downloading model (first use only)…")
        else:
            self.status_var.set("Separating…")

        threading.Thread(target=self._run_worker, args=(cmd,), daemon=True).start()

    def _cancel(self):
        p = self.proc
        if p is None:
            return
        self._cancelled = True
        self.cancel_btn.configure(state="disabled")
        self.status_var.set("Cancelling…")
        try:
            p.terminate()
        except Exception:
            pass

    @staticmethod
    def _quote(s):
        return f'"{s}"' if " " in s else s

    def _run_worker(self, cmd):
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            )
        except Exception as e:
            self.q.put(("line", f"Failed to launch: {e}\n"))
            self.q.put(("done", 1))
            return
        fd = self.proc.stdout
        cur = bytearray()
        while True:
            chunk = fd.read(4096)
            if not chunk:
                break
            for b in chunk:
                if b == 0x0A:  # \n -> committed line
                    self.q.put(("line", cur.decode("utf-8", "replace") + "\n"))
                    cur = bytearray()
                elif b == 0x0D:  # \r -> transient progress
                    self.q.put(("prog", cur.decode("utf-8", "replace")))
                    cur = bytearray()
                else:
                    cur.append(b)
        if cur:
            self.q.put(("line", cur.decode("utf-8", "replace") + "\n"))
        rc = self.proc.wait()
        self.q.put(("done", rc))

    # ---------- Queue / log ----------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._scan_health(payload)
                    self._log(payload)
                    if payload.strip():
                        self.status_var.set(payload.strip()[:120])
                elif kind == "prog":
                    text = payload.strip()
                    if text:
                        self._scan_health(text)
                        self.status_var.set(text[:120])
                        self._update_progress(text)
                elif kind == "models_loaded":
                    self.models = payload
                    self.model_by_file = {m["filename"]: m for m in payload}
                    self._scan_downloaded()
                    self._apply_filter()
                    self.refresh_btn.configure(state="normal")
                    preferred = self.settings.get("model") or DEFAULT_MODEL
                    if not self.tree.exists(preferred):
                        preferred = DEFAULT_MODEL
                    if self.tree.exists(preferred):
                        self.tree.selection_set(preferred)
                        self.tree.see(preferred)
                        self._on_model_select()
                    self.status_var.set(f"{len(payload)} models available.")
                    self._update_run_state()
                elif kind == "models_error":
                    self.refresh_btn.configure(state="normal")
                    self.status_var.set("Could not load models.")
                    self._log(f"Model list error: {payload}\n")
                elif kind == "update_result":
                    self._update_checking = False
                    res = payload
                    self.settings["last_update_check"] = time.time()
                    self._save_settings()
                    if res.get("ok") and res.get("newer"):
                        self._pending_update = res["info"]
                        self._render_update_available()
                        if res.get("manual"):
                            self.status_var.set(f"Version {res['info']['version']} is available.")
                    elif res.get("manual"):
                        if res.get("ok"):
                            messagebox.showinfo(
                                "Check for Updates",
                                f"You're up to date (version {APP_VERSION}).")
                        else:
                            messagebox.showerror(
                                "Check for Updates",
                                f"Couldn't check for updates:\n{res.get('error')}")
                        self.status_var.set(
                            f"{len(self.models)} models available." if self.models else "Ready.")
                elif kind == "update_prog":
                    if self.update_progress is not None:
                        done, total = payload
                        pct = (done / total * 100) if total else 0
                        self.update_progress.configure(value=min(100, pct))
                        tot = f" of {total / 1e6:.0f} MB" if total else ""
                        self.update_msg.configure(text=f"Downloading update… {done / 1e6:.0f} MB{tot}")
                elif kind == "update_ready":
                    self._update_downloading = False
                    if self.update_msg is not None:
                        self.update_msg.configure(text="Installing…")
                    self._install_update(payload)
                elif kind == "update_error":
                    self._update_downloading = False
                    messagebox.showerror("Update failed", str(payload))
                    self._render_update_available()
                elif kind == "update_cancelled":
                    self._update_downloading = False
                    self._render_update_available()
                elif kind == "done":
                    if (self._saw_memory_error and not self._chunked_retry
                            and not self._cancelled):
                        self._retry_with_chunking()
                    else:
                        self._finish(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_queue)

    def _scan_health(self, text):
        """Watch engine output for signals the exit code won't tell us about."""
        if any(marker in text for marker in MEM_ERROR_MARKERS):
            self._saw_memory_error = True
        if PROCESS_FAIL_MARKER in text:
            self._saw_process_failure = True

    def _retry_with_chunking(self):
        """A run ran out of GPU memory — re-run it once, splitting into chunks."""
        cmd = list(self._base_cmd)
        if "--chunk_duration" not in cmd:
            cmd += ["--chunk_duration", str(RETRY_CHUNK_SECONDS)]
        self._chunked_retry = True
        self._saw_memory_error = False
        self._saw_process_failure = False
        self._determinate = False
        self._log(
            f"\n⚠ Ran out of GPU memory processing the whole file at once. "
            f"Retrying in {RETRY_CHUNK_SECONDS}s chunks…\n\n"
        )
        self._log("$ " + " ".join(self._quote(c) for c in cmd) + "\n\n")
        self.status_var.set(f"Low on memory — retrying in {RETRY_CHUNK_SECONDS}s chunks…")
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        threading.Thread(target=self._run_worker, args=(cmd,), daemon=True).start()

    def _update_progress(self, text):
        m = PERCENT_RE.search(text)
        if not m:
            return
        pct = min(100.0, float(m.group(1)))
        if not self._determinate:
            self._determinate = True
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100)
        self.progress.configure(value=pct)

    def _finish(self, rc):
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self._determinate = False
        self.proc = None
        self.cancel_btn.configure(state="disabled")
        self.refresh_btn.configure(state="normal")
        cancelled = self._cancelled
        self._cancelled = False
        if cancelled:
            self.status_var.set("Cancelled.")
            self._log("\n■ Cancelled.\n")
        elif rc == 0 and not self._saw_process_failure:
            self.status_var.set("Done ✓  Your stems are ready.")
            self.open_btn.configure(state="normal")
            self._log("\n✓ Separation complete.\n")
            self._notify("Separation complete", "Your stems are ready.")
        else:
            if self._saw_memory_error:
                reason = ("Ran out of GPU memory even with chunking — try a shorter "
                          "file or a Mac with more memory.")
            elif self._saw_process_failure:
                reason = ("The engine reported an error and produced no output — "
                          "see the details below.")
            else:
                reason = f"Something went wrong (exit {rc}) — see the details below."
            self.status_var.set(reason)
            self._log(f"\n✗ Separation failed — {reason}\n")
            if not self.details_visible:
                self._toggle_details()
        self._scan_downloaded()
        self._apply_filter()
        self._update_run_state()

    def _notify(self, title, msg):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "{title}"'],
                capture_output=True, timeout=5)
        except Exception:
            pass

    def _open_output(self):
        d = getattr(self, "_last_outdir", None)
        if d and os.path.isdir(d):
            subprocess.run(["open", d])

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main():
    root = tk.Tk()
    SeparatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
