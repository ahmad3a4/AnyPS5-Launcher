"""AnyPS5 Launcher: add PS5 game folders, convert them with AnyPS5 and play them from one window.

Usage: pythonw "AnyPS5 Launcher.pyw" [--play <launcher game id or title id>]
A second start hands its request to the launcher that is already open.
"""
from __future__ import annotations

import collections
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anyps5core as core  # noqa: E402
import ui_theme as theme  # noqa: E402
from ui_dialogs import (AddGameDialog, ControlsDialog, ExportDialog, HistoryDialog, ImportDialog,  # noqa: E402
                        RemoveDialog, SavesDialog, ScanDialog, SettingsDialog, StorageDialog, WhatsNewDialog)
from ui_library import (FpsOverlay, GridView, HistoryChart, ListView, ScrollFrame, load_banner,  # noqa: E402
                        load_icon, placeholder, shorten)
from ui_theme import FONT, MONO, palette  # noqa: E402
from ui_console import ConsoleView  # noqa: E402

GPU_AUTO = "Automatic (dedicated GPU first)"
PRIORITY_LABELS = {"off": "Normal", "high": "High", "rt": "Real-time"}
FILTER_LABELS = ["Off", "Safe (1)", "Strict (2)"]
STATUS_FILTERS = {"All games": None, "Favorites": "favorite", "Ready to play": "ok", "Needs re-convert": "warn",
                  "Not converted": "idle", "Won't start yet": "blocked"}
TAG_FILTER = "Tag: "
SORTS = {"Name": "name", "Favorites first": "favorites", "Recently played": "recent", "Most played": "played",
         "Recently added": "added"}
CATEGORY_TEXT = {"running": "Running", "blocked": "Won't start yet", "ok": "Ready", "warn": "Re-convert", "idle": "Not converted"}
CATEGORY_LEVEL = {"running": "running", "blocked": "error", "ok": "ok", "warn": "warn", "idle": "idle"}
LOG_TAGS = {"cmd": "faint", "out": "text", "trace": "faint", "info": "running", "ok": "ok", "warn": "warn",
            "error": "error", "hint": "hint"}
LOG_VIEWS = {"Game output": "output", "Everything": "all", "Problems only": "problems"}
LOG_KEPT = 20000  # lines kept in memory for re-filtering; the full run is always in anyps5-last-run.log
MAX_LOG_LINES = 5000
DAY_MS = 24 * 3600 * 1000
SEARCH_HINT = "Search games"
SESSION_MINIMUM = 10  # seconds; shorter runs are failed starts, not play sessions


class App(tk.Tk):
    def __init__(self, instance: core.SingleInstance | None = None, play: str | None = None):
        super().__init__()
        self.withdraw()
        self.title("AnyPS5 Launcher")
        self.minsize(1100, 720)
        self.settings = core.LauncherSettings.load()
        self.library = core.Library()
        self.instance = instance
        self.installs: list[core.Install] = []
        self.events: queue.Queue = queue.Queue()
        self.job: tuple[str, threading.Event] | None = None
        self.process: core.GameProcess | None = None
        self.gpus: list[str] = []
        self.images: dict[tuple[str, str], tk.PhotoImage | None] = {}
        self.loading = False
        self.selected_id: str | None = self.settings.selected or None
        self.search_placeholder = True
        self.minimize_when_playing = False
        self.quick_launched = False
        self.continue_id: str | None = None
        self.overlay: FpsOverlay | None = None
        self.hotkey: core.Hotkey | None = None
        self.discord: core.DiscordPresence | None = None
        theme.apply(self, self.settings.theme)
        self.classic = ttk.Frame(self)  # the classic layout; the console view replaces it as a whole
        self.console: ConsoleView | None = None
        self._build()
        self.console = ConsoleView(self, self)
        self.reload_installs()
        self.refresh_library()
        self.geometry(self.settings.geometry or "1280x860")
        self.classic.pack(fill="both", expand=True)
        self.deiconify()
        theme.title_bar(self)
        self.update_idletasks()
        self.split_panes()
        if self.settings.home == "console":
            self.show_console(save=False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-f>", lambda _event: self.focus_search())
        self.bind("<F5>", lambda _event: self.refresh_library())
        self.after(40, self.poll)
        threading.Thread(target=self.detect_gpus, daemon=True).start()
        self.start_update_check()
        self.after(DAY_MS, self.periodic_update_check)
        self.log_startup()
        self.sync_shortcut_folders(quiet=True)  # refreshes icons of games converted since
        if play:
            self.after(400, lambda: self.quick_play(play))

    # ----- layout -----------------------------------------------------------

    def _build(self):
        self.header = ttk.Frame(self.classic, padding=(18, 14, 18, 8))
        self.header.pack(fill="x")
        header = self.header
        ttk.Label(header, text="AnyPS5", style="Brand.TLabel").pack(side="left")
        ttk.Label(header, text="Launcher", style="Muted.TLabel").pack(side="left", padx=(6, 22), pady=(5, 0))
        ttk.Label(header, text="Version", style="Muted.TLabel").pack(side="left")
        self.install_var = tk.StringVar()
        self.install_box = ttk.Combobox(header, textvariable=self.install_var, state="readonly", width=40)
        self.install_box.pack(side="left", padx=(8, 0))
        self.install_box.bind("<<ComboboxSelected>>", self.on_install_changed)
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(header, text="Console view", command=self.show_console).pack(side="right", padx=(0, 6))
        ttk.Button(header, text="Controls", command=self.open_controls).pack(side="right", padx=(0, 6))
        ttk.Button(header, text="Storage", command=self.open_storage).pack(side="right", padx=(0, 6))
        update = ttk.Menubutton(header, text="Update AnyPS5")
        self.update_menu = tk.Menu(update, tearoff=False, postcommand=self.fill_update_menu)
        update["menu"] = self.update_menu
        update.pack(side="right", padx=(0, 6))
        add = ttk.Menubutton(header, text="+ Add game", style="Accent.TMenubutton")
        add_menu = tk.Menu(add, tearoff=False)
        add_menu.add_command(label="Add a game folder…", command=self.add_game)
        add_menu.add_command(label="Scan a folder or drive for games…", command=self.scan_games)
        add["menu"] = add_menu
        add.pack(side="right", padx=(0, 12))
        self.continue_holder = ttk.Frame(header)
        self.continue_holder.pack(side="right", padx=(0, 8))
        self.continue_button = ttk.Button(self.continue_holder, text="", command=self.continue_last)

        self.banner = tk.Frame(self.classic, padx=18, pady=8)
        self.banner_text = tk.Label(self.banner, text="", font=(FONT, 10))
        self.banner_text.pack(side="left")
        ttk.Button(self.banner, text="Later", command=self.hide_banner).pack(side="right")
        ttk.Button(self.banner, text="Update now", style="Accent.TButton", command=self.update_from_source).pack(
            side="right", padx=(0, 6))
        ttk.Button(self.banner, text="What's new", command=self.whats_new).pack(side="right", padx=(0, 6))

        status = ttk.Frame(self.classic, padding=(18, 6))
        status.pack(side="bottom", fill="x")
        self.status_text = ttk.Label(status, text="Ready", style="Muted.TLabel")
        self.status_text.pack(side="left")
        self.cancel_button = ttk.Button(status, text="Cancel", command=self.cancel_job)
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=180)

        toolbar = ttk.Frame(self.classic, padding=(18, 2, 18, 10))
        toolbar.pack(fill="x")
        self.search_var = tk.StringVar(value=SEARCH_HINT)
        self.search = ttk.Entry(toolbar, textvariable=self.search_var, width=30, style="Placeholder.TEntry")
        self.search.pack(side="left")
        self.search.bind("<FocusIn>", lambda _event: self.search_focus(True))
        self.search.bind("<FocusOut>", lambda _event: self.search_focus(False))
        self.search.bind("<Escape>", lambda _event: (self.search_var.set(""), self.focus_set()))
        self.search_var.trace_add("write", lambda *_args: self.search_placeholder or self.refresh_library())
        self.filter_var = tk.StringVar(value="All games")
        self.status_filter = ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=20,
                                          values=list(STATUS_FILTERS))
        self.status_filter.pack(side="left", padx=(8, 0))
        self.status_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh_library())
        ttk.Label(toolbar, text="Sort", style="Muted.TLabel").pack(side="left", padx=(16, 6))
        self.sort_var = tk.StringVar(value=next((label for label, key in SORTS.items() if key == self.settings.sort), "Name"))
        sort = ttk.Combobox(toolbar, textvariable=self.sort_var, state="readonly", width=16, values=list(SORTS))
        sort.pack(side="left")
        sort.bind("<<ComboboxSelected>>", lambda _event: self.on_sort())
        self.list_button = ttk.Button(toolbar, text="☰  List", command=lambda: self.set_view("list"))
        self.list_button.pack(side="right")
        self.grid_button = ttk.Button(toolbar, text="▦  Grid", command=lambda: self.set_view("grid"))
        self.grid_button.pack(side="right", padx=(0, 4))
        self.count_label = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.count_label.pack(side="right", padx=(0, 14))

        panes = ttk.PanedWindow(self.classic, orient="vertical")
        panes.pack(fill="both", expand=True, padx=18)
        upper = self.upper = ttk.PanedWindow(panes, orient="horizontal")
        panes.add(upper, weight=5)
        self.library_host = ttk.Frame(upper)
        upper.add(self.library_host, weight=3)
        self.grid_view = GridView(self.library_host, self.select_game, self.activate_game, self.context_menu)
        self.list_view = ListView(self.library_host, self.select_game, self.activate_game, self.context_menu)
        self.empty = self._build_empty(self.library_host)
        self.details_host = ttk.Frame(upper, padding=(18, 0, 0, 0))
        upper.add(self.details_host, weight=4)
        self.notebook = ttk.Notebook(self.details_host)
        self.scrollers = [ScrollFrame(self.notebook, padding=(6, 14, 10, 10)) for _ in range(2)]
        self.overview, self.settings_tab = (scroller.body for scroller in self.scrollers)
        self.notebook.add(self.scrollers[0], text="Overview")
        self.notebook.add(self.scrollers[1], text="Game settings")
        self.shown_id: str | None = None
        # Wheel over a form scrolls the form; it never silently changes a drop-down's value.
        self.unbind_class("TCombobox", "<MouseWheel>")
        self.bind_all("<MouseWheel>", self.route_wheel, add="+")
        self.no_selection = ttk.Label(self.details_host, text="Select a game to see its details.", style="Empty.TLabel")
        self._build_overview()
        self._build_settings_tab()

        lower = ttk.Frame(panes)
        panes.add(lower, weight=1)
        bar = ttk.Frame(lower)
        bar.pack(fill="x", pady=(10, 4))
        ttk.Label(bar, text="Log", style="Heading.TLabel").pack(side="left")
        self.log_view_var = tk.StringVar(value="Game output")
        log_view = ttk.Combobox(bar, textvariable=self.log_view_var, state="readonly", width=14, values=list(LOG_VIEWS))
        log_view.pack(side="left", padx=(12, 0))
        log_view.bind("<<ComboboxSelected>>", lambda _event: self.render_log())
        self.log_search_var = tk.StringVar()
        log_search = ttk.Entry(bar, textvariable=self.log_search_var, width=24)
        log_search.pack(side="left", padx=(8, 0))
        log_search.bind("<Escape>", lambda _event: self.log_search_var.set(""))
        self.log_search_var.trace_add("write", lambda *_args: self.render_log())
        self.trace_label = ttk.Label(bar, text="", style="Small.TLabel")
        self.trace_label.pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Save…", command=self.save_log).pack(side="right")
        ttk.Button(bar, text="Clear", command=self.clear_log).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Copy report", command=self.copy_report).pack(side="right", padx=(0, 6))
        self.log_lines: collections.deque = collections.deque(maxlen=LOG_KEPT)
        self.trace_counts: collections.Counter = collections.Counter()
        frame = ttk.Frame(lower)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, height=9, wrap="word", font=(MONO, 9), relief="flat", padx=10, pady=8,
                                state="disabled", bd=0, highlightthickness=0)
        log_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        for tag in ("ok", "error", "hint", "info"):
            self.log_text.tag_configure(tag, font=(MONO, 9, "bold"))
        self.restyle_classic()

    def _build_empty(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=(6, 30, 6, 6))
        ttk.Label(frame, text="Add your first game", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, justify="left", wraplength=500, text=(
            "Pick a game's folder: the one that contains eboot.bin, sce_sys and sce_module, dumped from a game you "
            "own, or fake-signed homebrew, which the launcher unwraps for you.\n\nThe launcher converts the game with "
            "AnyPS5, links its files without copying them, and keeps your saves next to the converted game. The "
            "original folder is never changed.")).pack(anchor="w")
        ttk.Button(frame, text="+ Add game", style="Accent.TButton", command=self.add_game).pack(anchor="w", pady=(18, 0))
        return frame

    def _build_overview(self):
        o = self.overview
        self.banner_label = ttk.Label(o)
        self.overview_head = ttk.Frame(o)
        self.overview_head.pack(fill="x")
        self.icon_label = ttk.Label(self.overview_head)
        self.icon_label.pack(side="left", anchor="n")
        text = ttk.Frame(self.overview_head, padding=(16, 0, 0, 0))
        text.pack(side="left", fill="x", expand=True, anchor="n")
        self.title_label = ttk.Label(text, text="", style="Title.TLabel", wraplength=460)
        self.title_label.pack(anchor="w")
        subtitle_row = ttk.Frame(text)
        subtitle_row.pack(anchor="w", fill="x")
        self.subtitle_label = ttk.Label(subtitle_row, text="", style="Muted.TLabel")
        self.subtitle_label.pack(side="left")
        self.favorite_button = ttk.Button(subtitle_row, text="", style="Link.TButton", command=self.toggle_favorite)
        self.favorite_button.pack(side="left", padx=(12, 0))
        self.status_label = ttk.Label(text, text="", style="Status.TLabel", wraplength=460, justify="left")
        self.status_label.pack(anchor="w", pady=(8, 0))
        self.audit_label = ttk.Label(text, text="", wraplength=460, justify="left")
        self.audit_label.pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(o)
        actions.pack(fill="x", pady=(16, 0))
        self.play_button = ttk.Button(actions, text="▶  Play", style="Play.TButton", command=self.play)
        self.play_button.pack(side="left")
        self.convert_button = ttk.Button(actions, text="Convert", command=self.convert_selected)
        self.convert_button.pack(side="left", padx=(8, 0))
        self.audit_button = ttk.Button(actions, text="Check compatibility", command=self.audit_selected)
        self.audit_button.pack(side="left", padx=(6, 0))
        self.saves_button = ttk.Button(actions, text="Saves…", command=self.open_saves)
        self.saves_button.pack(side="left", padx=(6, 0))
        more = ttk.Menubutton(actions, text="More")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="Create desktop shortcut", command=self.create_shortcut)
        menu.add_command(label="Compatibility history…", command=self.open_history)
        menu.add_separator()
        menu.add_command(label="Open converted folder", command=lambda: self.open_path("output"))
        menu.add_command(label="Open original folder", command=lambda: self.open_path("source"))
        menu.add_command(label="Open last run log", command=lambda: self.open_path("log"))
        menu.add_command(label="Change game folder…", command=self.change_game_folder)
        menu.add_separator()
        menu.add_command(label="Remove from library…", command=self.remove_selected)
        more["menu"] = menu
        more.pack(side="left", padx=(6, 0))

        stats = ttk.Frame(o)
        stats.pack(fill="x", pady=(18, 0))
        self.stat_values: dict[str, ttk.Label] = {}
        for key, label in (("played", "Played"), ("sessions", "Sessions"), ("last_session", "Last session"),
                           ("last_played", "Last played"), ("backups", "Save backups")):
            block = ttk.Frame(stats)
            block.pack(side="left", padx=(0, 28))
            ttk.Label(block, text=label, style="Small.TLabel").pack(anchor="w")
            value = ttk.Label(block, text="—", style="Stat.TLabel")
            value.pack(anchor="w")
            self.stat_values[key] = value
        self.performance_label = ttk.Label(o, text="", style="Small.TLabel", wraplength=520, justify="left")
        self.performance_label.pack(anchor="w", pady=(8, 0))

        compat = ttk.Frame(o)
        compat.pack(fill="x", pady=(20, 0))
        row = ttk.Frame(compat)
        row.pack(fill="x")
        ttk.Label(row, text="Missing functions per check", style="Heading.TLabel").pack(side="left")
        ttk.Button(row, text="Full history", style="Link.TButton", command=self.open_history).pack(side="left", padx=(8, 0))
        self.chart_card = tk.Frame(compat, padx=10, pady=6)
        self.chart = HistoryChart(self.chart_card, 380, 104)
        self.chart.pack()
        self.trend_label = ttk.Label(compat, text="", style="Small.TLabel", wraplength=460, justify="left")
        self.trend_label.pack(anchor="w", pady=(4, 0))
        shots = ttk.Frame(o)
        shots.pack(fill="x", pady=(20, 0))
        heading_row = ttk.Frame(shots)
        heading_row.pack(fill="x")
        ttk.Label(heading_row, text="Screenshots", style="Heading.TLabel").pack(side="left")
        self.screenshot_button = ttk.Button(heading_row, text="Take one now", style="Link.TButton", command=self.screenshot)
        self.screenshot_button.pack(side="left", padx=(8, 0))
        ttk.Button(heading_row, text="Open folder", style="Link.TButton", command=self.open_screenshots).pack(side="left", padx=(4, 0))
        self.gallery = ttk.Frame(shots)
        self.gallery.pack(anchor="w", pady=(6, 0))
        self.gallery_note = ttk.Label(shots, text="", style="Small.TLabel")
        self.gallery_note.pack(anchor="w")
        self.thumbnails: dict[tuple[str, float], tk.PhotoImage] = {}

        notes = ttk.Frame(o)
        notes.pack(fill="x", pady=(20, 0))
        notes.columnconfigure(1, weight=1)
        ttk.Label(notes, text="Tags and notes", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(notes, text="Tags").grid(row=1, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
        self.tags_var = tk.StringVar()
        tags = self.tags_entry = ttk.Entry(notes, textvariable=self.tags_var)
        tags.grid(row=1, column=1, sticky="we", pady=(6, 0))
        tags.bind("<FocusOut>", lambda _event: self.store_organizing())
        tags.bind("<Return>", lambda _event: self.store_organizing())
        ttk.Label(notes, text="e.g. platformer, co-op; filter by them above the library", style="Small.TLabel").grid(
            row=2, column=1, sticky="w")
        ttk.Label(notes, text="Notes").grid(row=3, column=0, sticky="nw", pady=(6, 0), padx=(0, 10))
        self.notes_text = tk.Text(notes, height=4, wrap="word", font=(FONT, 10), relief="flat", padx=8, pady=6, bd=0,
                                  highlightthickness=1, undo=True)
        self.notes_text.grid(row=3, column=1, sticky="we", pady=(6, 0))
        self.notes_text.bind("<FocusOut>", lambda _event: self.store_organizing())
        self.notes_text.bind("<KeyRelease>", lambda _event: self.schedule_notes_save())
        self.notes_job: str | None = None
        self.paths_label = ttk.Label(o, text="", style="Small.TLabel", justify="left", wraplength=560)
        self.paths_label.pack(anchor="w", pady=(16, 0))

    def _build_settings_tab(self):
        box = self.settings_tab
        box.columnconfigure(1, weight=1)
        self.var_gpu = tk.StringVar()
        self.var_language = tk.StringVar()
        self.var_intel = tk.BooleanVar()
        self.var_priority = tk.StringVar()
        self.var_flip = tk.BooleanVar()
        self.var_filter = tk.StringVar()
        self.var_diagnostics = tk.BooleanVar()
        self.var_modules = tk.StringVar()
        self.var_env = tk.StringVar()
        self.var_fullscreen = tk.BooleanVar()
        row = 0

        def add(widget, label=None, note=None, pady=4):
            nonlocal row
            if label:
                ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=pady, padx=(0, 14))
                widget.grid(row=row, column=1, sticky="we", pady=pady)
            else:
                widget.grid(row=row, column=0, columnspan=3, sticky="w", pady=pady)
            if note:
                ttk.Label(box, text=note, style="Small.TLabel").grid(row=row, column=2, sticky="w", padx=(8, 0))
            row += 1

        add(ttk.Label(box, text="Playing", style="Heading.TLabel"), pady=(0, 6))
        self.gpu_box = ttk.Combobox(box, textvariable=self.var_gpu, state="readonly", values=[GPU_AUTO], width=34)
        add(self.gpu_box, "Graphics card")
        add(ttk.Combobox(box, textvariable=self.var_language, state="readonly", values=core.LANGUAGES), "Console language")
        add(ttk.Combobox(box, textvariable=self.var_priority, state="readonly", values=list(PRIORITY_LABELS.values())),
            "Thread priority")
        add(ttk.Checkbutton(box, text="Start in fullscreen (presses F11 for you once the game shows its first frames)",
                            variable=self.var_fullscreen))
        add(ttk.Checkbutton(box, text="Store GPU results at each flip (fixes some corrupted graphics, a little slower)",
                            variable=self.var_flip))
        add(ttk.Entry(box, textvariable=self.var_env), "Environment", "KEY=value; KEY2=value")
        controls = ttk.Frame(box)
        self.controls_label = ttk.Label(controls, text="")
        self.controls_label.pack(side="left")
        ttk.Button(controls, text="Customize for this game…", command=self.open_game_controls).pack(side="left", padx=(10, 0))
        add(controls, "Keyboard & mouse")
        add(ttk.Label(box, text="Conversion", style="Heading.TLabel"), pady=(18, 0))
        add(ttk.Label(box, text="Changing these needs a re-convert.", style="Small.TLabel"), pady=(0, 6))
        add(ttk.Checkbutton(box, variable=self.var_intel, text="Intel CPU fix: replace AMD-only instructions the PS5 CPU has"))
        add(ttk.Combobox(box, textvariable=self.var_filter, state="readonly", values=FILTER_LABELS), "Unused import filter")
        add(ttk.Entry(box, textvariable=self.var_modules), "Extra module folders", "e.g. Media/Plugins; separate with ;")
        add(ttk.Checkbutton(box, text="Startup diagnostics (more detail when a game will not start)", variable=self.var_diagnostics))
        for variable in (self.var_gpu, self.var_language, self.var_intel, self.var_priority, self.var_flip,
                         self.var_filter, self.var_diagnostics, self.var_modules, self.var_env, self.var_fullscreen):
            variable.trace_add("write", lambda *_args: self.store_settings())

    def restyle_classic(self):
        p = palette
        self.log_text.configure(bg=p["log"], fg=p["text"], insertbackground=p["text"], selectbackground=p["selection"])
        for tag, role in LOG_TAGS.items():
            self.log_text.tag_configure(tag, foreground=p[role])
        self.banner.configure(bg=p["banner"])
        self.banner_text.configure(bg=p["banner"], fg=p["text"])
        self.notes_text.configure(bg=p["surface"], fg=p["text"], insertbackground=p["text"], selectbackground=p["selection"],
                                  highlightbackground=p["border"], highlightcolor=p["accent"])
        self.chart_card.configure(bg=p["surface"])
        self.grid_button.configure(style="ToggleOn.TButton" if self.settings.view == "grid" else "Toggle.TButton")
        self.list_button.configure(style="ToggleOn.TButton" if self.settings.view == "list" else "Toggle.TButton")

    def route_wheel(self, event):
        for scroller in self.scrollers:
            if scroller.contains(event.widget):
                scroller.wheel(event)

    def split_panes(self, fraction: float = 0.4):
        width = self.upper.winfo_width()
        if width > 1:
            self.upper.sashpos(0, int(width * fraction))

    def apply_theme(self):
        theme.apply(self, self.settings.theme)
        self.images.clear()
        self.restyle_classic()
        for scroller in self.scrollers:
            scroller.restyle()
        self.grid_view.restyle()
        self.list_view.restyle()
        self.refresh_library()

    # ----- state ------------------------------------------------------------

    def current_install(self) -> core.Install | None:
        return next((item for item in self.installs if item.name == self.settings.install), self.installs[0] if self.installs else None)

    @staticmethod
    def install_label(install: core.Install) -> str:
        built = re.match(r"AnyPS5 main (\w+) (\d{4}-\d{2}-\d{2})", install.label)
        if built:
            return f"{install.name}  ·  main {built.group(1)}, {built.group(2)}"
        release = re.match(r"AnyPS5 (v[\w.-]+)", install.label)
        return f"{install.name}  ·  {release.group(1)} release" if release else install.name

    def reload_installs(self):
        self.installs = core.find_installs()
        labels = [self.install_label(item) for item in self.installs]
        self.install_box["values"] = labels
        install = self.current_install()
        if install:
            self.settings.install = install.name
            self.install_var.set(labels[self.installs.index(install)])

    def on_install_changed(self, _event=None):
        index = self.install_box.current()
        if 0 <= index < len(self.installs):
            self.settings.install = self.installs[index].name
            self.settings.save()
            self.refresh_library()

    def selected_game(self) -> core.Game | None:
        return self.library.get(self.selected_id) if self.selected_id else None

    def is_running(self, game: core.Game | None = None) -> bool:
        running = self.process is not None and self.process.running
        return running and (game is None or self.process.game.id == game.id)

    def category(self, game: core.Game) -> str:
        if self.is_running(game):
            return "running"
        _, level = core.conversion_status(game, self.current_install())
        return "blocked" if level == "ok" and core.will_not_start(game) else level

    def image(self, game: core.Game, kind: str):
        key = (game.id, kind)
        if key not in self.images:
            if kind == "banner":
                pic = game.source / "sce_sys" / "pic0.png"
                self.images[key] = load_banner(pic, 560, 132) if pic.is_file() else None
            else:
                size = {"tile": 128, "small": 32, "large": 96}[kind]
                self.images[key] = load_icon(game.icon, size) if game.icon.is_file() else placeholder(size)
        return self.images[key]

    def library_item(self, game: core.Game) -> dict:
        category = self.category(game)
        played = core.format_duration(game.playtime) if game.playtime else ""
        return {"id": game.id, "title": ("★ " if game.favorite else "") + game.title, "title_id": game.title_id or "",
                "status": CATEGORY_TEXT[category],
                "level": CATEGORY_LEVEL[category], "detail": f"Played {played}" if played else "Not played yet",
                "image": self.image(game, "tile"), "small": self.image(game, "small"), "played": played,
                "last": (game.last_played or "")[:10]}

    def matches_filter(self, game: core.Game) -> bool:
        choice = self.filter_var.get()
        if choice.startswith(TAG_FILTER):
            return choice[len(TAG_FILTER):].lower() in (tag.lower() for tag in game.tags)
        wanted = STATUS_FILTERS.get(choice)
        if wanted == "favorite":
            return game.favorite
        return wanted is None or self.category(game) == wanted

    def visible_games(self) -> list[core.Game]:
        query = "" if self.search_placeholder else self.search_var.get()
        games = [game for game in self.library.games if core.matches_search(game, query) and self.matches_filter(game)]
        order = self.settings.sort
        by_name = sorted(games, key=lambda game: game.title.lower())
        if order == "recent":
            return sorted(by_name, key=lambda game: game.last_played or "", reverse=True)
        if order == "played":
            return sorted(by_name, key=lambda game: game.playtime, reverse=True)
        if order == "added":
            return sorted(by_name, key=lambda game: game.added or "", reverse=True)
        if order == "favorites":
            return sorted(by_name, key=lambda game: not game.favorite)
        return by_name

    def refresh_filter_choices(self):
        choices = list(STATUS_FILTERS) + [TAG_FILTER + tag for tag in core.all_tags(self.library)]
        self.status_filter["values"] = choices
        if self.filter_var.get() not in choices:
            self.filter_var.set("All games")

    def active_view(self):
        return self.grid_view if self.settings.view == "grid" else self.list_view

    def refresh_library(self, details: bool = True):
        self.refresh_filter_choices()
        games = self.visible_games()
        view, other = (self.grid_view, self.list_view) if self.settings.view == "grid" else (self.list_view, self.grid_view)
        other.pack_forget()
        if not self.library.games:
            view.pack_forget()
            self.empty.pack(fill="both", expand=True)
        else:
            self.empty.pack_forget()
            view.pack(fill="both", expand=True)
        ids = [game.id for game in games]
        if self.selected_id not in ids:
            self.selected_id = ids[0] if ids else None
        view.selected = self.selected_id
        view.set_items([self.library_item(game) for game in games], "No games match the search or filter." if not games else "")
        view.select(self.selected_id)
        total = len(self.library.games)
        self.count_label.configure(text=f"{len(games)} of {total} games" if len(games) != total else
                                   f"{total} game{'' if total == 1 else 's'}")
        self.refresh_continue()
        if details:
            self.show_details()
        else:
            self.update_buttons()
        if self.console is not None and self.console.visible:
            self.console.refresh()

    def show_console(self, save: bool = True):
        self.classic.pack_forget()
        self.console.show()
        if save:
            self.settings.home = "console"
            self.settings.save()

    def show_classic(self):
        self.console.hide()
        self.classic.pack(fill="both", expand=True)
        self.settings.home = "classic"
        self.settings.save()
        self.refresh_library()
        self.update_idletasks()
        self.split_panes()

    def refresh_continue(self):
        played = [game for game in self.library.games if game.last_played]
        last = max(played, key=lambda game: game.last_played) if played else None
        self.continue_id = last.id if last else None
        if last:
            self.continue_button.configure(text=f"▶  Continue {shorten(last.title, 26)}")
            self.continue_button.pack()
        else:
            self.continue_button.pack_forget()

    def set_view(self, view: str):
        position = self.upper.sashpos(0)
        self.settings.view = view
        self.settings.save()
        self.restyle_classic()
        self.refresh_library()
        self.after_idle(lambda: self.upper.sashpos(0, position))  # the list's wider table must not squeeze the details

    def on_sort(self):
        self.settings.sort = SORTS.get(self.sort_var.get(), "name")
        self.settings.save()
        self.refresh_library()

    def search_focus(self, entering: bool):
        if entering and self.search_placeholder:
            self.search_placeholder = False
            self.search.configure(style="TEntry")
            self.search_var.set("")
        elif not entering and not self.search_var.get():
            self.search_placeholder = True
            self.search.configure(style="Placeholder.TEntry")
            self.search_var.set(SEARCH_HINT)

    def focus_search(self):
        self.search.focus_set()
        self.search.select_range(0, "end")

    def clear_filters(self):
        self.filter_var.set("All games")
        if not self.search_placeholder:
            self.search_var.set("")
            self.search_focus(False)

    def select_game(self, key: str):
        self.selected_id = key
        self.show_details()

    def activate_game(self, key: str):
        self.selected_id = key
        self.show_details()
        self.play()

    def show_details(self):
        game = self.selected_game()
        if game is None:
            self.notebook.pack_forget()
            if self.library.games:
                self.no_selection.pack(anchor="nw", pady=40)
            else:
                self.no_selection.pack_forget()
            self.update_buttons()
            return
        self.no_selection.pack_forget()
        self.notebook.pack(fill="both", expand=True)
        fresh = game.id != self.shown_id
        if fresh:
            self.store_organizing()  # keep what was typed for the previous game
            self.shown_id = game.id
            for scroller in self.scrollers:
                scroller.to_top()
        self.favorite_button.configure(text="★ Favorite" if game.favorite else "☆ Add to favorites")
        typing = self.focus_get() in (self.tags_entry, self.notes_text)
        if fresh or not typing:
            self.tags_var.set(", ".join(game.tags))
            self.notes_text.delete("1.0", "end")
            self.notes_text.insert("1.0", game.notes)
            self.notes_text.edit_reset()
        own = core.has_own_controls(game)
        self.controls_label.configure(text="Its own layout" if own else "The layout shared by all games")
        self.loading = True
        try:
            banner = self.image(game, "banner")
            if banner is not None:
                self.banner_label.configure(image=banner)
                self.banner_label.pack(fill="x", pady=(0, 14), before=self.overview_head)
            else:
                self.banner_label.pack_forget()
            self.icon_label.configure(image=self.image(game, "large"))
            self.title_label.configure(text=game.title)
            self.subtitle_label.configure(text="  ·  ".join(part for part in (game.title_id, game.version and f"v{game.version}") if part))
            self.update_status_labels(game)
            backups = core.list_backups(game)
            values = {"played": core.format_duration(game.playtime) if game.sessions else "—",
                      "sessions": str(game.sessions) if game.sessions else "—",
                      "last_session": core.format_duration(game.last_session) if game.sessions else "—",
                      "last_played": game.last_played[:10] if game.last_played else "Never",
                      "backups": str(len(backups)) if backups else "None"}
            for key, value in values.items():
                self.stat_values[key].configure(text=value)
            self.performance_label.configure(text=core.performance_text(game) or
                                             "Frame rate is measured while you play (after the first 10 seconds).")
            self.fill_gallery(game)
            if game.audit_history:
                self.chart_card.pack(anchor="w", pady=(6, 0), before=self.trend_label)
                self.chart.draw(game.audit_history)
                self.trend_label.configure(text=core.history_trend(game) or
                                           "One check so far. Re-check after updating AnyPS5 to see the trend.")
            else:
                self.chart_card.pack_forget()
                self.trend_label.configure(text="Run Check compatibility to start tracking how many functions this game is missing.")
            self.paths_label.configure(text=f"Game folder: {game.source_dir}\nConverted to: {game.output_dir}")
            settings = game.settings
            self.gpu_box["values"] = [GPU_AUTO] + self.gpus + ([settings.gpu] if settings.gpu and settings.gpu not in self.gpus else [])
            self.var_gpu.set(settings.gpu or GPU_AUTO)
            self.var_language.set(core.LANGUAGES[settings.language] if 0 <= settings.language < len(core.LANGUAGES) else core.LANGUAGES[1])
            self.var_priority.set(PRIORITY_LABELS.get(settings.thread_priority, "Normal"))
            self.var_flip.set(settings.store_at_flip)
            self.var_intel.set(settings.to_intel)
            self.var_filter.set(FILTER_LABELS[settings.unused_filter] if 0 <= settings.unused_filter < 3 else FILTER_LABELS[0])
            self.var_diagnostics.set(settings.diagnostics)
            self.var_modules.set(settings.module_dirs)
            self.var_env.set(settings.env)
            self.var_fullscreen.set(settings.fullscreen)
        finally:
            self.loading = False
        self.update_buttons()

    def update_status_labels(self, game: core.Game):
        text, level = (("Running", "running") if self.is_running(game) else core.conversion_status(game, self.current_install()))
        if game.converted and level == "ok":
            text += f"  ·  converted {game.converted.get('at', '')} with {game.converted.get('install', '')}"
        self.status_label.configure(text=text, foreground=palette[level])
        if game.audit:
            verdict, verdict_level = core.audit_verdict(game.audit)
            improved = game.audit.get("improved_count", 0)
            if improved:
                names = ", ".join(game.audit.get("improved", [])[:4])
                verdict += (f". Since the check before, {improved} function{'s' if improved != 1 else ''} it uses "
                            f"became available ({names}{'…' if improved > 4 else ''})")
            self.audit_label.configure(text=verdict, foreground=palette["ok" if verdict_level == "ok" else verdict_level])
        else:
            self.audit_label.configure(text="Compatibility not checked yet.", foreground=palette["muted"])

    def store_settings(self):
        if self.loading:
            return
        game = self.selected_game()
        if game is None:
            return
        settings = game.settings
        gpu = self.var_gpu.get()
        settings.gpu = "" if gpu == GPU_AUTO else gpu
        if self.var_language.get() in core.LANGUAGES:
            settings.language = core.LANGUAGES.index(self.var_language.get())
        settings.thread_priority = next((key for key, label in PRIORITY_LABELS.items() if label == self.var_priority.get()), "off")
        settings.store_at_flip = self.var_flip.get()
        settings.to_intel = self.var_intel.get()
        settings.unused_filter = FILTER_LABELS.index(self.var_filter.get()) if self.var_filter.get() in FILTER_LABELS else 0
        settings.diagnostics = self.var_diagnostics.get()
        settings.module_dirs = self.var_modules.get()
        settings.env = self.var_env.get()
        settings.fullscreen = self.var_fullscreen.get()
        self.library.save()
        self.update_status_labels(game)
        self.refresh_library(details=False)

    def toggle_favorite(self, game: core.Game | None = None):
        game = game or self.selected_game()
        if game is None:
            return
        game.favorite = not game.favorite
        self.library.save()
        self.refresh_library()

    def schedule_notes_save(self):
        if self.notes_job is not None:
            self.after_cancel(self.notes_job)
        self.notes_job = self.after(800, self.store_organizing)

    def store_organizing(self):
        """Saves the tags and notes typed for the game shown in the details panel."""
        self.notes_job = None
        game = self.library.get(self.shown_id) if self.shown_id else None
        if game is None:
            return
        tags = core.parse_tags(self.tags_var.get())
        notes = self.notes_text.get("1.0", "end-1c").rstrip()
        if tags == game.tags and notes == game.notes:
            return
        retag = tags != game.tags
        game.tags, game.notes = tags, notes
        self.library.save()
        if retag:
            self.tags_var.set(", ".join(tags))
            self.refresh_library(details=False)

    def update_buttons(self):
        game = self.selected_game()
        busy = self.job is not None
        running = self.is_running()
        this_running = self.is_running(game) if game else False
        self.play_button.configure(text="■  Stop" if this_running else "▶  Play")
        self.play_button.state(["!disabled"] if game and (this_running or (not busy and not running)) else ["disabled"])
        for button in (self.convert_button,):
            button.state(["!disabled"] if game and not busy and not this_running else ["disabled"])
        self.saves_button.state(["!disabled"] if game else ["disabled"])
        self.screenshot_button.state(["!disabled"] if this_running else ["disabled"])
        has_source = (Path(self.settings.source_dir) / "tools" / "import_audit.py").is_file()
        converted = game is not None and (game.output / "eboot.registry.json").is_file()
        self.audit_button.state(["!disabled"] if game and not busy and has_source and converted else ["disabled"])

    # ----- log and events ---------------------------------------------------

    def log(self, text: str, tag: str = "info"):
        """Thread-safe: queued and written by the Tk thread."""
        self.events.put(("log", text, tag))

    def log_visible(self, text: str, tag: str) -> bool:
        view = LOG_VIEWS.get(self.log_view_var.get(), "output")
        if view == "output" and tag == "trace":
            return False
        if view == "problems" and tag not in ("warn", "error", "hint"):
            return False
        query = self.log_search_var.get().strip().lower()
        return not query or query in text.lower()

    def write_log(self, lines: list[tuple[str, str]]):
        """Keeps every line for re-filtering, shows the ones the current view and search allow."""
        shown = []
        for text, tag in lines:
            self.log_lines.append((text, tag))
            if tag == "trace":
                self.trace_counts[core.trace_name(text) or "other"] += 1
            if self.log_visible(text, tag):
                shown.append((text, tag))
        if shown:
            self.insert_log(shown)
        self.update_trace_label()

    def insert_log(self, lines: list[tuple[str, str]]):
        at_end = self.log_text.yview()[1] >= 0.999
        self.log_text.configure(state="normal")
        for text, tag in lines:
            self.log_text.insert("end", text + "\n", tag)
        excess = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.configure(state="disabled")
        if at_end:
            self.log_text.see("end")  # follow new output, unless the reader scrolled up

    def render_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        visible = [(text, tag) for text, tag in self.log_lines if self.log_visible(text, tag)]
        self.insert_log(visible[-MAX_LOG_LINES:])
        self.log_text.see("end")
        self.update_trace_label()

    def update_trace_label(self):
        hidden = sum(self.trace_counts.values())
        if hidden and LOG_VIEWS.get(self.log_view_var.get()) != "all":
            top = ", ".join(f"{name} {count}" for name, count in self.trace_counts.most_common(3))
            self.trace_label.configure(text=f"{hidden} runtime trace lines hidden ({top})")
        else:
            self.trace_label.configure(text="")

    def copy_report(self):
        game = self.process.game if self.is_running() else self.selected_game()
        if game is None:
            messagebox.showinfo("Copy report", "Select a game first.", parent=self)
            return
        report = core.build_report(game, self.current_install(), list(self.log_lines))
        self.clipboard_clear()
        self.clipboard_append(report)
        self.log(f"Report for {game.title} copied to the clipboard ({len(report.splitlines())} lines)", "ok")

    def log_startup(self):
        install = self.current_install()
        if install is None:
            self.log("No AnyPS5 installation was found next to the launcher. Use Update AnyPS5 > Download latest official release.", "warn")
        else:
            count = len(self.library.games)
            self.log(f"Using {install.name} ({install.label}). {count} game{'' if count == 1 else 's'} in the library.", "info")
        if core.cpu_is_intel():
            self.log("Intel CPU detected: new games get the Intel CPU fix automatically.", "info")

    def poll(self):
        lines = []
        try:
            for _ in range(2000):
                event = self.events.get_nowait()
                if event[0] == "log":
                    lines.append((event[1], event[2]))
                    continue
                if lines:
                    self.write_log(lines)
                    lines = []
                try:
                    self.handle_event(event)
                except Exception:  # noqa: BLE001 - one bad event must not stop the loop
                    self.write_log([(traceback.format_exc().rstrip(), "error")])
        except queue.Empty:
            pass
        if lines:
            self.write_log(lines)
        self.after(40, self.poll)

    def handle_event(self, event):
        kind = event[0]
        if kind == "job_done":
            _, name, ok, on_done, on_fail = event
            self.job = None
            self.progress.stop()
            self.progress.pack_forget()
            self.cancel_button.pack_forget()
            self.status_text.configure(text=f"{name}: {'done' if ok else 'failed'}")
            self.library.save()
            self.refresh_library()
            if ok and on_done:
                on_done()
            elif not ok and on_fail:
                on_fail()
        elif kind == "game_exit":
            self.on_game_exit(event[1], event[2])
        elif kind == "frame":
            self.on_frame(*event[1:])
        elif kind == "screenshot_key":
            self.screenshot()
        elif kind == "screenshot_saved":
            game = self.selected_game()
            if game is not None and game.id == event[1]:
                self.fill_gallery(game)
        elif kind == "gpus":
            self.gpus = event[1]
            if self.selected_game():
                self.show_details()
        elif kind == "updates":
            self.on_updates(*event[1:])
        elif kind == "remote":
            self.on_remote(event[1])

    def start_job(self, name: str, work, on_done=None, on_fail=None):
        if self.job is not None:
            messagebox.showinfo("Busy", f"Wait for '{self.job[0]}' to finish first.", parent=self)
            return
        cancel = threading.Event()
        self.job = (name, cancel)
        self.status_text.configure(text=f"{name}…")
        self.cancel_button.pack(side="right")
        self.progress.pack(side="right", padx=(0, 8))
        self.progress.start(12)
        self.update_buttons()

        def worker():
            ok = False
            try:
                work(cancel)
                ok = True
            except core.LauncherError as error:
                self.log(str(error), "error")
            except Exception:  # noqa: BLE001 - reported in the log rather than lost in a thread
                self.log(traceback.format_exc().rstrip(), "error")
            self.events.put(("job_done", name, ok, on_done, on_fail))

        threading.Thread(target=worker, daemon=True).start()

    def cancel_job(self):
        if self.job:
            self.job[1].set()
            self.log("Cancelling…", "warn")

    # ----- games ------------------------------------------------------------

    def add_game(self):
        folder = filedialog.askdirectory(parent=self, title="Pick the game's folder (the one with eboot.bin and sce_sys)")
        if not folder:
            return
        try:
            info = core.inspect_game_folder(Path(folder))
        except core.LauncherError as error:
            messagebox.showerror("Cannot add this folder", str(error), parent=self)
            return
        output = AddGameDialog(self, info).show()
        if output is None:
            return
        if not core.can_hardlink(info.folder, output):
            size = core.human_size(core.folder_size(info.folder))
            if not messagebox.askyesno("Files will be copied", (
                    f"The converted folder is on another drive (or a drive without hard links), so the game's files "
                    f"will be copied instead of linked: {size}.\n\nPick a folder on the same drive as the game to avoid "
                    "this. Copy anyway?"), parent=self):
                return
        try:
            game = self.library.add(info, output, to_intel=core.cpu_is_intel())
        except core.LauncherError as error:
            messagebox.showerror("Cannot add this game", str(error), parent=self)
            return
        self.log(f"Added {game.title}" + (" (Intel CPU detected: Intel CPU fix enabled)" if game.settings.to_intel else ""), "info")
        self.clear_filters()
        self.selected_id = game.id
        self.refresh_library()
        self.convert_game(game, then=self.sync_shortcut_folders)  # after converting, so shortcuts get the game's icon

    def convert_selected(self):
        game = self.selected_game()
        if game:
            self.convert_game(game)

    def convert_game(self, game: core.Game, then=None):
        install = self.current_install()
        if install is None:
            messagebox.showerror("No AnyPS5", "No AnyPS5 installation found. Use Update AnyPS5 first.", parent=self)
            return
        source = Path(self.settings.source_dir)

        def work(cancel):
            core.convert(game, install, self.log, cancel)
            self.library.save()
            if (source / "tools" / "import_audit.py").is_file():
                self.log("Checking compatibility…", "info")
                try:
                    core.audit(game, source, self.log)
                except core.LauncherError as error:
                    self.log(f"Compatibility check skipped: {error}", "warn")
                self.library.save()

        self.start_job(f"Converting {game.title}", work, then)

    def convert_many(self, games: list[core.Game], title: str, audit_only: bool = False, then=None):
        install, source = self.current_install(), Path(self.settings.source_dir)
        can_audit = (source / "tools" / "import_audit.py").is_file()
        running_id = self.process.game.id if self.is_running() else None

        def work(cancel):
            failures = []
            for index, game in enumerate(games, 1):
                if cancel.is_set():
                    raise core.LauncherError("Cancelled")
                if game.id == running_id:
                    self.log(f"Skipped {game.title}: it is running", "warn")
                    continue
                self.log(f"[{index}/{len(games)}] {game.title}", "info")
                try:
                    if not audit_only:
                        core.convert(game, install, self.log, cancel)
                    if can_audit:
                        core.audit(game, source, self.log)
                except core.LauncherError as error:
                    failures.append(game.title)
                    self.log(str(error), "error")
                self.library.save()
            done = len(games) - len(failures)
            self.log(f"{title}: {done} of {len(games)} done" + (f"; failed: {', '.join(failures)}" if failures else ""),
                     "warn" if failures else "ok")

        self.start_job(title, work, then)

    def audit_selected(self):
        game = self.selected_game()
        if game:
            self.start_job(f"Checking {game.title}",
                           lambda _cancel: (core.audit(game, Path(self.settings.source_dir), self.log), self.library.save()))

    def quick_play(self, key: str, minimize: bool = True):
        game = core.find_game(self.library, key)
        if game is None:
            self.deiconify()
            messagebox.showerror("Game not found", f"There is no game '{key}' in the library. The shortcut may be "
                                 "from a game you removed.", parent=self)
            return
        self.clear_filters()
        self.selected_id = game.id
        self.refresh_library()
        if self.is_running(game):
            return  # a second click on the shortcut only brings the launcher forward; Play would toggle to Stop
        self.minimize_when_playing = minimize
        self.play(game)

    def continue_last(self):
        game = self.library.get(self.continue_id) if self.continue_id else None
        if game:
            self.clear_filters()
            self.selected_id = game.id
            self.refresh_library()
            self.play(game)

    def play(self, game: core.Game | None = None):
        game = game or self.selected_game()
        minimize, self.minimize_when_playing = self.minimize_when_playing, False
        if game is None:
            return
        if self.is_running():
            if self.is_running(game):
                self.process.stop()
            else:
                messagebox.showinfo("A game is running", f"Stop {self.process.game.title} first.", parent=self)
            return
        if self.job is not None:
            return
        text, level = core.conversion_status(game, self.current_install())

        def after_convert():
            self.minimize_when_playing = minimize
            self.play(game)

        if level == "idle":
            self.convert_game(game, then=after_convert)
            return
        if level == "warn":
            answer = messagebox.askyesnocancel("Re-convert first?", f"{text}.\n\nRe-convert now (recommended)? "
                                               "Choose No to play the current conversion.", parent=self)
            if answer is None:
                return
            if answer:
                self.convert_game(game, then=after_convert)
                return
        if core.will_not_start(game):
            verdict, _ = core.audit_verdict(game.audit)
            if not messagebox.askyesno("This game will not start yet", (
                    f"{verdict}.\n\nThese are gaps in AnyPS5 itself: no setting fixes them, and the game stops as soon "
                    "as Windows loads it. Use Update AnyPS5 from time to time and re-check.\n\nLaunch anyway?"),
                    icon="warning", default="no", parent=self):
                return
        if self.settings.auto_backup:
            self.auto_backup(game, "before")
        if self.settings.fps_overlay and self.overlay is None:
            self.overlay = FpsOverlay(self)  # shown now, before the game window exists, so it never steals focus
        try:
            self.process = core.GameProcess(game, self.log, lambda code: self.events.put(("game_exit", game.id, code)),
                                            lambda fps, origin: self.events.put(("frame", game.id, fps, origin)))
        except (core.LauncherError, OSError) as error:
            self.close_overlay()
            messagebox.showerror("Cannot start", str(error), parent=self)
            return
        game.last_played = time.strftime("%Y-%m-%d %H:%M")
        self.library.save()
        self.status_text.configure(text=f"Playing {game.title}")
        self.start_hotkey()
        self.discord_show(game)
        if minimize:
            self.quick_launched = True
            self.iconify()
        self.refresh_library()

    def auto_backup(self, game: core.Game, when: str):
        try:
            backup = core.backup_saves(game, "auto")
        except (OSError, core.LauncherError) as error:
            self.log(f"Automatic save backup failed: {error}", "warn")
            return
        if backup:
            self.log(f"Saves backed up automatically {when} playing ({core.human_size(backup.size)})", "info")

    def close_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def on_frame(self, game_id: str, fps: float, origin: tuple[int, int]):
        if not self.is_running() or self.process.game.id != game_id:
            return
        self.status_text.configure(text=f"Playing {self.process.game.title}  ·  {fps:.1f} FPS")
        if self.overlay is not None:
            self.overlay.show(fps, origin)
        if self.console is not None and self.console.visible and self.console.selected() is self.process.game:
            self.console.set_fps(fps)

    def on_game_exit(self, game_id: str, code: int):
        process = self.process
        game = self.library.get(game_id)
        self.close_overlay()
        self.stop_hotkey()
        self.discord_clear()
        if game is not None and process is not None:
            if process.elapsed >= SESSION_MINIMUM:
                core.record_session(game, process.elapsed)
                self.log(f"Session {core.format_duration(process.elapsed)}; {game.title} played "
                         f"{core.format_duration(game.playtime)} in total", "info")
            if process.performance:
                core.record_performance(game, process.performance)
                performance = process.performance
                self.log(f"Frame rate: {performance['avg']} FPS average, lowest {performance['low']}, "
                         f"highest {performance['high']}", "info")
            if self.settings.auto_backup:
                self.auto_backup(game, "after")
            self.library.save()
        clean = process is None or process.stopped or (code == 0 and not process.output.hinted)
        if self.quick_launched:
            self.quick_launched = False
            if self.state() == "iconic":
                if clean:
                    self.on_close(force=True)
                    return
                self.deiconify()
                self.lift()
                self.focus_force()
        self.status_text.configure(text="Ready")
        self.refresh_library()

    def remove_selected(self):
        game = self.selected_game()
        if game is None:
            return
        if self.is_running(game):
            messagebox.showinfo("Game running", "Stop the game before removing it.", parent=self)
            return
        answer = RemoveDialog(self, game).show()
        if not answer:
            return
        try:
            self.library.remove(game, answer["delete"])
        except (core.LauncherError, OSError) as error:
            messagebox.showerror("Remove", str(error), parent=self)
            return
        for kind in ("tile", "small", "large", "banner"):
            self.images.pop((game.id, kind), None)
        self.log(f"Removed {game.title}" + (" and its converted folder (saves kept in the launcher's backups)"
                                            if answer["delete"] else ""), "info")
        self.selected_id = None
        self.refresh_library()
        self.sync_shortcut_folders()

    def context_menu(self, key: str, event):
        self.select_game(key)
        game = self.selected_game()
        if game is None:
            return
        busy = self.job is not None
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Stop" if self.is_running(game) else "Play", command=self.play)
        menu.add_command(label="Convert", command=self.convert_selected, state="disabled" if busy else "normal")
        menu.add_command(label="Check compatibility", command=self.audit_selected,
                         state="disabled" if "disabled" in self.audit_button.state() else "normal")
        menu.add_command(label="Saves…", command=self.open_saves)
        menu.add_command(label="Remove from favorites" if game.favorite else "Add to favorites",
                         command=lambda: self.toggle_favorite(game))
        menu.add_command(label="Create desktop shortcut", command=self.create_shortcut)
        menu.add_separator()
        menu.add_command(label="Open converted folder", command=lambda: self.open_path("output"))
        menu.add_command(label="Remove from library…", command=self.remove_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def sync_shortcut_folders(self, remove_disabled: bool = False, quiet: bool = False):
        """Brings the "AnyPS5 Games" folders (Start menu, desktop) in step with the library, in the background."""
        targets = [(self.settings.start_menu_folder, core.start_menu_dir(), "Start menu"),
                   (self.settings.desktop_folder, core.desktop_games_dir(), "desktop")]
        if not remove_disabled and not any(enabled for enabled, _, _ in targets):
            return
        games = list(self.library.games)

        def work():
            for enabled, folder, where in targets:
                try:
                    if enabled:
                        written, _ = core.sync_shortcut_folder(games, folder)
                        if not quiet:
                            self.log(f'"{folder.name}" in the {where}: {written - 1} game shortcuts', "ok")
                    elif remove_disabled and folder.exists():
                        core.remove_shortcut_folder(folder)
                        self.log(f'Removed "{folder.name}" from the {where}', "info")
                except (core.LauncherError, OSError, subprocess.SubprocessError) as error:
                    self.log(f"Shortcut folder in the {where} not updated: {error}", "warn")

        threading.Thread(target=work, daemon=True).start()

    def open_saves(self):
        game = self.selected_game()
        if game:
            SavesDialog(self, game, self.is_running, self.log).show()
            self.show_details()

    def open_history(self):
        game = self.selected_game()
        if game is None:
            return
        if not game.audit_history:
            messagebox.showinfo("No history yet", "Run Check compatibility first; every check adds a point.", parent=self)
            return
        HistoryDialog(self, game).show()

    def create_shortcut(self):
        game = self.selected_game()
        if game is None:
            return
        try:
            link = core.create_game_shortcut(game)
        except (core.LauncherError, OSError, subprocess.SubprocessError) as error:
            messagebox.showerror("Shortcut", f"Could not create the shortcut: {error}", parent=self)
            return
        self.log(f"Desktop shortcut created: {link}. Double-click it to play {game.title} directly.", "ok")

    def open_path(self, which: str):
        game = self.selected_game()
        if game is None:
            return
        path = {"output": game.output, "source": game.source, "log": game.output / core.RUN_LOG}[which]
        if not path.exists():
            messagebox.showinfo("Not there yet", f"{path} does not exist yet.", parent=self)
            return
        core.open_in_explorer(path)

    # ----- launcher ---------------------------------------------------------

    def open_settings(self):
        result = SettingsDialog(self, self.settings).show()
        if result and result["theme"]:
            self.apply_theme()
        if result and result["folders"]:
            self.sync_shortcut_folders(remove_disabled=True)
        self.update_buttons()

    def open_controls(self):
        if ControlsDialog(self).show():
            self.log(f"Controls for all games saved to {core.INPUT_FILE}" if core.INPUT_FILE.is_file() else
                     "Controls for all games reset to the built-in bindings", "ok")

    def open_game_controls(self):
        game = self.selected_game()
        if game is None:
            return
        result = ControlsDialog(self, game).show()
        if result:
            self.log(f"{game.title} now has its own keyboard and mouse layout" if result["path"] else
                     f"{game.title} uses the layout shared by all games again", "ok")
            self.show_details()

    def scan_games(self):
        chosen = ScanDialog(self, self.library).show()
        if not chosen:
            return
        needs_copy = [info for info, output in chosen if not core.can_hardlink(info.folder, output)]
        if needs_copy:
            size = core.human_size(sum(core.folder_size(info.folder) for info in needs_copy))
            if not messagebox.askyesno("Files will be copied", (
                    f"{len(needs_copy)} of the games cannot be hard-linked where they are going (another drive, or a "
                    f"drive without hard links), so their files will be copied: {size}. Continue?"), parent=self):
                return
        added, failed = [], []
        for info, output in chosen:
            try:
                added.append(self.library.add(info, output, to_intel=core.cpu_is_intel()))
            except core.LauncherError as error:
                failed.append(f"{info.title}: {error}")
        for message in failed:
            self.log(message, "warn")
        if not added:
            return
        self.log(f"Added {len(added)} game{'s' if len(added) != 1 else ''}; converting them now", "info")
        self.clear_filters()
        self.selected_id = added[0].id
        self.refresh_library()
        self.convert_many(added, f"Converting {len(added)} added games", then=self.sync_shortcut_folders)

    def open_storage(self):
        StorageDialog(self, self.library, self.is_running, self.log).show()

    # ----- what's new, screenshots, Discord, moving ---------------------------

    def whats_new(self):
        repo = Path(self.settings.source_dir)
        if not (repo / ".git").exists():
            messagebox.showinfo("What's new", f"No AnyPS5 source checkout at {repo}; set it in Settings.", parent=self)
            return
        if WhatsNewDialog(self, repo, list(self.library.games)).show():
            self.update_from_source()

    def screenshot(self):
        if not self.is_running():
            return
        game, pid = self.process.game, self.process.process.pid

        def work():
            try:
                shot = core.take_screenshot(game, pid)
            except (core.LauncherError, OSError) as error:
                self.log(f"Screenshot failed: {error}", "warn")
                return
            if os.name == "nt":
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            self.log(f"Screenshot saved: {shot}", "ok")
            self.events.put(("screenshot_saved", game.id))

        threading.Thread(target=work, daemon=True).start()

    def start_hotkey(self):
        self.stop_hotkey()
        if self.settings.screenshot_key and os.name == "nt":
            self.hotkey = core.Hotkey(self.settings.screenshot_hotkey, self.screenshot_from_key)
            if not self.hotkey.start():
                self.log(f"The screenshot key is off for this session: {self.hotkey.error}. Pick another key in "
                         "Settings > Playing; 'Take one now' in the Overview still works.", "warn")
                self.hotkey = None

    def screenshot_from_key(self):
        self.events.put(("screenshot_key",))  # the hot key thread is not the Tk thread

    def stop_hotkey(self):
        if getattr(self, "hotkey", None) is not None:
            self.hotkey.stop()
        self.hotkey = None

    def discord_show(self, game: core.Game):
        app_id = self.settings.discord_app_id
        if not app_id:
            return
        if self.discord is None or self.discord.app_id != app_id:
            if self.discord is not None:
                self.discord.close()
            self.discord = core.DiscordPresence(app_id)
        self.discord.show(game.title, time.time())

        def report():
            if self.discord is not None and self.discord.last_error and self.is_running(game):
                self.log(f"Discord status not shown: {self.discord.last_error}", "warn")

        self.after(4000, report)

    def discord_clear(self):
        if self.discord is not None:
            self.discord.clear()

    def open_screenshots(self):
        game = self.selected_game()
        if game is None:
            return
        folder = core.screenshots_dir(game)
        if not folder.is_dir():
            messagebox.showinfo("No screenshots yet", f"Press {self.settings.screenshot_hotkey} while the game runs "
                                "(or Take one now in the Overview).", parent=self)
            return
        core.open_in_explorer(folder)

    def fill_gallery(self, game: core.Game):
        for child in self.gallery.winfo_children():
            child.destroy()
        shots = core.list_screenshots(game)
        if not shots:
            key = (f"{self.settings.screenshot_hotkey} or Take one now" if self.settings.screenshot_key
                   else "Take one now")
            self.gallery_note.configure(text=f"No screenshots yet. While the game runs, press {key}.")
            return
        self.gallery_note.configure(text=f"{len(shots)} screenshot{'s' if len(shots) != 1 else ''}. "
                                         "Click one to open it; right-click to delete." if len(shots) <= 6 else
                                    f"Newest 6 of {len(shots)}. Click one to open it; right-click to delete.")
        for index, shot in enumerate(shots[:6]):
            thumb = core.screenshot_thumbnail(shot)
            source = thumb if thumb.is_file() else shot
            key = (str(source), source.stat().st_mtime)
            if key not in self.thumbnails:
                try:
                    image = tk.PhotoImage(file=str(source))
                    factor = max(1, round(image.width() / 160))
                    self.thumbnails[key] = image.subsample(factor) if factor > 1 else image
                except tk.TclError:
                    continue
            label = ttk.Label(self.gallery, image=self.thumbnails[key], cursor="hand2")
            label.grid(row=index // 3, column=index % 3, padx=(0, 8), pady=(0, 8), sticky="nw")
            label.bind("<Button-1>", lambda _event, path=shot: core.open_in_explorer(path))
            label.bind("<Button-3>", lambda event, path=shot: self.screenshot_menu(event, path))

    def screenshot_menu(self, event, shot: Path):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Open", command=lambda: core.open_in_explorer(shot))
        menu.add_command(label="Show in folder", command=lambda: subprocess.Popen(["explorer", "/select,", str(shot)]))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.delete_screenshot(shot))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def delete_screenshot(self, shot: Path):
        if messagebox.askyesno("Delete screenshot", f"Delete {shot.name}?", parent=self):
            core.delete_screenshot(shot)
            game = self.selected_game()
            if game:
                self.fill_gallery(game)

    def change_game_folder(self):
        game = self.selected_game()
        if game is None or self.is_running(game):
            return
        folder = filedialog.askdirectory(parent=self, title=f"New folder of {game.title}")
        if not folder:
            return
        try:
            info = core.inspect_game_folder(Path(folder))
        except core.LauncherError as error:
            messagebox.showerror("Change game folder", str(error), parent=self)
            return
        if game.title_id and info.title_id and info.title_id != game.title_id and not messagebox.askyesno(
                "Different game?", f"That folder holds {info.title} ({info.title_id}), not {game.title} ({game.title_id}). "
                                   "Use it anyway?", parent=self):
            return
        core.relocate_game(game, Path(folder))
        self.library.save()
        self.log(f"{game.title} now uses {game.source_dir}; convert it again to play", "info")
        self.images = {key: value for key, value in self.images.items() if key[0] != game.id}
        self.refresh_library()

    def export_launcher(self, parent):
        choice = ExportDialog(parent, self.library).show()
        parent.grab_set()
        if not choice:
            return
        target = choice["target"]

        def work(_cancel):
            counts = core.export_bundle(self.library, self.settings, target, choice["screenshots"], self.log)
            self.log(f"Exported {counts['games']} games, {counts['backups']} save backups, {counts['controls']} control "
                     f"layouts and {counts['screenshots']} screenshots to {target}", "ok")

        self.start_job("Exporting the launcher", work)

    def import_launcher(self, parent):
        source = filedialog.askopenfilename(parent=parent, filetypes=[("Launcher export", "*.zip")])
        if not source:
            return
        try:
            bundle = core.read_bundle(Path(source))
        except core.LauncherError as error:
            messagebox.showerror("Import", str(error), parent=parent)
            return
        relocations = ImportDialog(parent, Path(source), bundle, self.library).show()
        parent.grab_set()
        if relocations is None:
            return
        try:
            added = core.import_bundle(Path(source), self.library, self.settings, relocations, self.log)
        except (core.LauncherError, OSError) as error:
            messagebox.showerror("Import", str(error), parent=parent)
            return
        self.log(f"Imported {len(added)} games. Convert each to play it; its saves come back after the first conversion.", "ok")
        self.refresh_library()

    def fill_update_menu(self):
        menu = self.update_menu
        menu.delete(0, "end")
        menu.add_command(label="What's new in AnyPS5…", command=self.whats_new)
        menu.add_separator()
        menu.add_command(label="Build latest from source", command=self.update_from_source)
        menu.add_command(label="Download latest official release", command=self.download_release)
        menu.add_command(label="Check for updates now", command=lambda: self.start_update_check(announce=True))
        menu.add_separator()
        outdated = core.outdated_games(self.library, self.current_install())
        converted = [game for game in self.library.games if game.converted]
        menu.add_command(label=f"Re-convert outdated games ({len(outdated)})", command=self.reconvert_outdated,
                         state="normal" if outdated and self.job is None else "disabled")
        menu.add_command(label=f"Re-check compatibility of all games ({len(converted)})", command=self.recheck_all,
                         state="normal" if converted and self.job is None else "disabled")

    def update_from_source(self, keep_edits: bool = True):
        repo, toolchain = Path(self.settings.source_dir), Path(self.settings.toolchain_dir)
        if keep_edits and not messagebox.askyesno("Update AnyPS5", (
                f"Pull the newest AnyPS5 source in {repo}, rebuild it and install it as {core.LATEST_INSTALL_NAME}?\n\n"
                "This takes a minute or several. Converted games keep working; the launcher offers to re-convert "
                "them afterwards."), parent=self):
            return
        destination = core.WORKSPACE_DIR / core.LATEST_INSTALL_NAME

        def failed():
            try:
                edited = core.local_edits(repo) if keep_edits else []
            except (core.LauncherError, OSError):
                edited = []
            if edited and messagebox.askyesno("Build the official AnyPS5?", (
                    f"The update did not build. The AnyPS5 source in {repo} has edits that are not part of AnyPS5 "
                    f"({len(edited)} file{'s' if len(edited) != 1 else ''}: {', '.join(edited[:3])}"
                    f"{'…' if len(edited) > 3 else ''}), and they are the likely cause.\n\n"
                    "Build the official AnyPS5 instead? Your edits are not deleted: they are kept in git's stash, and "
                    "the log says how to bring them back. Your current AnyPS5 keeps working either way."), parent=self):
                self.update_from_source(keep_edits=False)

        def done():
            self.settings.install = core.LATEST_INSTALL_NAME
            self.reload_installs()
            self.hide_banner()
            self.settings.notified_revision = ""
            self.settings.save()
            self.refresh_library()
            outdated = core.outdated_games(self.library, self.current_install())
            if outdated and messagebox.askyesno("Re-convert games?", (
                    f"AnyPS5 was updated. Re-convert {len(outdated)} game{'s' if len(outdated) != 1 else ''} now so they "
                    "use it? Saves are not touched."), parent=self):
                self.convert_many(outdated, "Re-converting games")

        self.start_job("Updating AnyPS5" if keep_edits else "Building the official AnyPS5",
                       lambda cancel: core.update_from_source(repo, toolchain, destination, self.log, cancel, keep_edits),
                       done, failed)

    def reconvert_outdated(self):
        outdated = core.outdated_games(self.library, self.current_install())
        if outdated:
            self.convert_many(outdated, "Re-converting games")

    def recheck_all(self):
        converted = [game for game in self.library.games if game.converted and (game.output / "eboot.registry.json").is_file()]
        if converted:
            self.convert_many(converted, "Re-checking compatibility", audit_only=True)

    def download_release(self):
        self.start_job("Downloading the latest release",
                       lambda _cancel: core.download_latest_release(core.WORKSPACE_DIR, self.log), self.reload_installs)

    def start_update_check(self, announce: bool = False):
        repo = Path(self.settings.source_dir)
        if not (repo / ".git").exists():
            if announce:
                self.log(f"No AnyPS5 source checkout at {repo}; set it in Settings.", "warn")
            return

        def work():
            try:
                count = core.check_source_updates(repo)
                revision = core.upstream_revision(repo) if count else ""
            except (core.LauncherError, OSError) as error:
                if announce:
                    self.log(f"Update check failed: {error}", "warn")
                return
            self.events.put(("updates", count, revision, announce))

        threading.Thread(target=work, daemon=True).start()

    def periodic_update_check(self):
        self.start_update_check()
        self.after(DAY_MS, self.periodic_update_check)

    def on_updates(self, count: int, revision: str, announce: bool):
        self.settings.last_update_check = time.time()
        plural = "s" if count != 1 else ""
        if count:
            self.show_banner(f"AnyPS5 has {count} new change{plural} since your build. Update to get the newest fixes and functions.")
            if self.settings.notify_updates and revision and revision != self.settings.notified_revision:
                core.windows_toast("AnyPS5 update available", f"{count} new change{plural} in AnyPS5. "
                                   "Open the launcher and choose Update now.")
                self.settings.notified_revision = revision
        else:
            self.hide_banner()
        if announce:
            self.log(f"{count} new change{plural} in AnyPS5 since your build." if count else "AnyPS5 is up to date.", "ok")
        self.settings.save()

    def show_banner(self, text: str):
        self.banner_text.configure(text=text)
        self.banner.pack(fill="x", after=self.header)

    def hide_banner(self):
        self.banner.pack_forget()

    def on_remote(self, message: dict):
        self.deiconify()
        self.lift()
        self.focus_force()
        if message.get("command") == "play" and message.get("game"):
            self.quick_play(str(message["game"]), minimize=False)

    def detect_gpus(self):
        self.events.put(("gpus", core.list_gpus()))

    def clear_log(self):
        self.log_lines.clear()
        self.trace_counts.clear()
        self.render_log()

    def save_log(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt", initialfile="anyps5-launcher-log.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")

    def on_close(self, force: bool = False):
        if not force:
            if self.is_running():
                if not messagebox.askyesno("Quit", f"{self.process.game.title} is running. Stop it and quit?", parent=self):
                    return
                self.process.stop()
            if self.job is not None:
                if not messagebox.askyesno("Quit", f"'{self.job[0]}' is still running. Quit anyway?", parent=self):
                    return
                self.job[1].set()
        self.close_overlay()
        self.stop_hotkey()
        if self.discord is not None:
            self.discord.close()
        self.store_organizing()
        self.settings.selected = self.selected_id or ""
        if self.state() == "normal":
            self.settings.geometry = self.geometry()
        self.settings.save()
        if self.instance is not None:
            self.instance.close()
        self.destroy()


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    play = argv[argv.index("--play") + 1] if "--play" in argv and argv.index("--play") + 1 < len(argv) else None
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    instance = core.SingleInstance()
    if instance.forward({"command": "play", "game": play} if play else {"command": "show"}):
        return
    app = App(instance, play)
    instance.serve(lambda message: app.events.put(("remote", message)))
    app.mainloop()


if __name__ == "__main__":
    main()
