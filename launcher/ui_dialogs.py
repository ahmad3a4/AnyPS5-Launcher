"""Dialogs of the AnyPS5 launcher."""
from __future__ import annotations

import os
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import anyps5core as core
import ui_theme as theme
from ui_library import HistoryChart, load_icon
from ui_theme import FONT, palette

TK_TO_SDL = {
    "Return": "Return", "space": "Space", "Escape": "Escape", "BackSpace": "Backspace", "Tab": "Tab",
    "Shift_L": "Left Shift", "Shift_R": "Right Shift", "Control_L": "Left Ctrl", "Control_R": "Right Ctrl",
    "Alt_L": "Left Alt", "Alt_R": "Right Alt", "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "Prior": "PageUp", "Next": "PageDown", "Home": "Home", "End": "End", "Insert": "Insert", "Delete": "Delete",
    "Caps_Lock": "CapsLock", "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]", "semicolon": ";",
    "apostrophe": "'", "comma": ",", "period": ".", "slash": "/", "backslash": "\\", "grave": "`",
    "KP_Enter": "Keypad Enter", "KP_Add": "Keypad +", "KP_Subtract": "Keypad -", "KP_Multiply": "Keypad *",
    "KP_Divide": "Keypad /", "KP_Decimal": "Keypad .",
}
BACKUP_KINDS = {"manual": "Manual", "auto": "Automatic", "before-restore": "Before a restore",
                "before-remove": "Before removing the game", "imported": "Imported"}


def sdl_key_name(keysym: str) -> str | None:
    if keysym in TK_TO_SDL:
        return TK_TO_SDL[keysym]
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.upper()
    if keysym.startswith("KP_") and keysym[3:].isdigit():
        return "Keypad " + keysym[3:]
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym
    return None


class Dialog(tk.Toplevel):
    """Modal window centred on its parent, in the current palette."""

    def __init__(self, master: tk.Misc, title: str, resizable: bool = False):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.transient(master)
        self.resizable(resizable, resizable)
        self.configure(background=palette["bg"])
        self.result = None
        self.body = ttk.Frame(self, padding=18)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda _event: self.destroy())

    def buttons(self, *specs) -> ttk.Frame:
        """Right-aligned row; specs are (text, command, accent) with the primary action first."""
        row = ttk.Frame(self.body)
        for text, command, accent in reversed(specs):
            ttk.Button(row, text=text, command=command, style="Accent.TButton" if accent else "TButton").pack(
                side="right", padx=(6, 0))
        return row

    def show(self):
        self.update_idletasks()
        master = self.master
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        theme.title_bar(self)
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result


class AddGameDialog(Dialog):
    def __init__(self, master: tk.Misc, info: core.GameInfo):
        super().__init__(master, "Add game")
        self.info = info
        self.icon = load_icon(info.folder / "sce_sys" / "icon0.png", 96)
        ttk.Label(self.body, image=self.icon).grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 16))
        ttk.Label(self.body, text=info.title, style="Title.TLabel").grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Label(self.body, text=" · ".join(part for part in (info.title_id, info.version and f"v{info.version}") if part),
                  style="Muted.TLabel").grid(row=1, column=1, columnspan=2, sticky="w")
        kind = "plain ELF" if info.kind == "elf" else "fake-signed SELF (it will be unwrapped)"
        modules = ", ".join(info.module_dirs + info.suggested_module_dirs) or "none"
        ttk.Label(self.body, text=f"Executable: {info.executable.name}, {kind}\nModule folders: {modules}",
                  justify="left").grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(self.body, text="Converted game goes to:").grid(row=4, column=0, columnspan=3, sticky="w", pady=(18, 4))
        self.output = tk.StringVar(value=str(core.default_output_dir(info.folder)))
        ttk.Entry(self.body, textvariable=self.output, width=64).grid(row=5, column=0, columnspan=2, sticky="we")
        ttk.Button(self.body, text="Browse…", command=self.browse).grid(row=5, column=2, padx=(6, 0))
        ttk.Label(self.body, text="Saves are stored in this folder too. The original game folder is never changed.",
                  style="Small.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.buttons(("Add and convert", self.accept, True), ("Cancel", self.destroy, False)).grid(
            row=7, column=0, columnspan=3, sticky="e", pady=(18, 0))

    def browse(self):
        folder = filedialog.askdirectory(parent=self, title="Folder for the converted game")
        if folder:
            self.output.set(str(Path(folder) / f"{self.info.folder.name}-anyps5"))

    def accept(self):
        text = self.output.get().strip()
        if text:
            self.result = Path(text)
            self.destroy()


class RemoveDialog(Dialog):
    def __init__(self, master: tk.Misc, game: core.Game):
        super().__init__(master, "Remove game")
        ttk.Label(self.body, text=f"Remove {game.title} from the library?", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.body, text="The original game folder is never touched.", style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        self.delete = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(self.body, variable=self.delete, text="Also delete the converted folder")
        check.pack(anchor="w")
        ttk.Label(self.body, text=f"{game.output}\nYour saves are backed up to the launcher first, so they can be restored "
                                  "if you add the game again.", style="Small.TLabel", wraplength=460,
                  justify="left").pack(anchor="w", padx=(26, 0))
        if not (game.output / core.MARKER_FILE).is_file():
            check.state(["disabled"])
        self.buttons(("Remove", self.accept, True), ("Cancel", self.destroy, False)).pack(anchor="e", pady=(18, 0))

    def accept(self):
        self.result = {"delete": self.delete.get()}
        self.destroy()


class SettingsDialog(Dialog):
    """Launcher settings in four tabs, so the window stays small enough for any screen."""

    def __init__(self, master, settings: core.LauncherSettings):
        super().__init__(master, "Launcher settings")
        self.settings = settings
        self.theme = tk.StringVar(value=settings.theme)
        self.auto_backup = tk.BooleanVar(value=settings.auto_backup)
        self.notify = tk.BooleanVar(value=settings.notify_updates)
        self.fps_overlay = tk.BooleanVar(value=settings.fps_overlay)
        self.screenshot_key = tk.BooleanVar(value=settings.screenshot_key)
        self.screenshot_hotkey = tk.StringVar(value=settings.screenshot_hotkey if settings.screenshot_hotkey in core.HOTKEYS
                                              else core.DEFAULT_HOTKEY)
        self.discord = tk.StringVar(value=settings.discord_app_id)
        self.start_menu = tk.BooleanVar(value=settings.start_menu_folder)
        self.desktop = tk.BooleanVar(value=settings.desktop_folder)
        self.source = tk.StringVar(value=settings.source_dir)
        self.toolchain = tk.StringVar(value=settings.toolchain_dir)
        notebook = ttk.Notebook(self.body)
        notebook.pack(fill="both", expand=True)
        tabs = {}
        for name in ("General", "Playing", "Library", "AnyPS5 & folders"):
            tab = ttk.Frame(notebook, padding=(4, 14, 4, 4))
            tab.columnconfigure(0, weight=1)
            notebook.add(tab, text=name)
            tabs[name] = [tab, 0]

        def heading(tab, text):
            frame, row = tabs[tab]
            ttk.Label(frame, text=text, style="Heading.TLabel").grid(row=row, column=0, columnspan=2, sticky="w",
                                                                     pady=(0 if row == 0 else 16, 4))
            tabs[tab][1] += 1

        def place(tab, widget, sticky="w", pady=0, padx=0, columnspan=2, column=0):
            frame, row = tabs[tab]
            widget.grid(row=row, column=column, columnspan=columnspan, sticky=sticky, pady=pady, padx=padx)
            tabs[tab][1] += 1

        def note(tab, text, padx=26):
            place(tab, ttk.Label(tabs[tab][0], text=text, style="Small.TLabel", wraplength=500, justify="left"), padx=(padx, 0))

        general = tabs["General"][0]
        heading("General", "Appearance")
        themes = ttk.Frame(general)
        for value, label in (("system", "Match Windows"), ("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(themes, text=label, value=value, variable=self.theme).pack(side="left", padx=(0, 16))
        place("General", themes)
        heading("General", "Updates")
        place("General", ttk.Checkbutton(general, variable=self.notify,
                                         text="Tell me when AnyPS5 has new changes (checked at start and daily)"))
        checked = time.strftime("%Y-%m-%d %H:%M", time.localtime(settings.last_update_check)) if settings.last_update_check else "never"
        note("General", f"Last checked: {checked}")
        heading("General", "Saves")
        place("General", ttk.Checkbutton(general, variable=self.auto_backup,
                                         text=f"Back up saves before and after each session (keeps the last {core.AUTO_BACKUPS_KEPT})"))
        heading("General", "Move to another PC")
        moving = ttk.Frame(general)
        ttk.Button(moving, text="Export…", command=lambda: self.master.export_launcher(self)).pack(side="left")
        ttk.Button(moving, text="Import…", command=lambda: self.master.import_launcher(self)).pack(side="left", padx=(6, 0))
        place("General", moving)
        note("General", "Library, settings, controls, playtime, tags, notes and saves in one file.", padx=0)

        playing = tabs["Playing"][0]
        heading("Playing", "While a game runs")
        place("Playing", ttk.Checkbutton(playing, variable=self.fps_overlay,
                                         text="Show an FPS counter over the game (it never takes the game's focus)"))
        keys = ttk.Frame(playing)
        ttk.Checkbutton(keys, variable=self.screenshot_key, text="Save a screenshot with").pack(side="left")
        ttk.Combobox(keys, textvariable=self.screenshot_hotkey, values=list(core.HOTKEYS), state="readonly", width=12).pack(
            side="left", padx=(6, 0))
        place("Playing", keys, pady=(6, 0))
        note("Playing", f"Screenshots go to {core.pictures_dir()}. F12 cannot be used: Windows keeps it for its debugger.")
        heading("Playing", "Discord status")
        discord = ttk.Frame(playing)
        ttk.Label(discord, text="Application ID").pack(side="left")
        ttk.Entry(discord, textvariable=self.discord, width=26).pack(side="left", padx=(8, 0))
        place("Playing", discord)
        note("Playing", "Shows the game you play on your Discord profile. Discord needs an application to show it under: "
                        "open discord.com/developers/applications, choose New Application, name it AnyPS5 (Discord shows "
                        "that name), and paste its Application ID here. Leave it empty to keep this off.", padx=0)

        library = tabs["Library"][0]
        heading("Library", "Shortcut folders")
        place("Library", ttk.Checkbutton(library, variable=self.start_menu,
                                         text=f'Keep an "{core.SHORTCUT_FOLDER}" folder in the Start menu'))
        place("Library", ttk.Checkbutton(library, variable=self.desktop,
                                         text=f'Keep an "{core.SHORTCUT_FOLDER}" folder on the desktop'))
        note("Library", "One shortcut per game, kept in step with the library; double-clicking one plays the game.")
        heading("Library", "Fonts")
        basic, cjk = core.installed_fonts()
        self.fonts_label = ttk.Label(library, text="", style="Small.TLabel")
        self.update_fonts_label(basic, cjk)
        place("Library", self.fonts_label, columnspan=1)
        frame, row = tabs["Library"]
        ttk.Button(library, text="Download free fonts…", command=self.fonts).grid(row=row - 1, column=1, sticky="e", padx=(6, 0))
        note("Library", "Games that open the console's system fonts show no text without them.", padx=0)

        paths = tabs["AnyPS5 & folders"][0]
        heading("AnyPS5 & folders", "AnyPS5 source and toolchain")
        for label, variable in (("AnyPS5 source checkout (Update and Check compatibility use it)", self.source),
                                ("MinGW toolchain bin folder (Update uses it)", self.toolchain)):
            place("AnyPS5 & folders", ttk.Label(paths, text=label), pady=(8, 2))
            frame, row = tabs["AnyPS5 & folders"]
            ttk.Entry(paths, textvariable=variable, width=56).grid(row=row, column=0, sticky="we")
            ttk.Button(paths, text="Browse…", command=lambda v=variable: self.browse(v)).grid(row=row, column=1, padx=(6, 0))
            tabs["AnyPS5 & folders"][1] += 1
        heading("AnyPS5 & folders", "Open a folder")
        folders = ttk.Frame(paths)
        for label, folder in (("Fonts", core.FONTS_DIR), ("Save backups", core.BACKUPS_DIR),
                              ("Screenshots", core.pictures_dir()), ("Launcher data", core.DATA_DIR)):
            ttk.Button(folders, text=label, command=lambda f=folder: self.open_folder(f)).pack(side="left", padx=(0, 6))
        place("AnyPS5 & folders", folders)
        self.buttons(("Save", self.accept, True), ("Cancel", self.destroy, False)).pack(anchor="e", pady=(16, 0))

    def update_fonts_label(self, basic: int, cjk: int):
        self.fonts_label.configure(text=f"{basic} of {len(core.FONT_FILES)} free substitute fonts installed"
                                        + (f", plus {cjk} Japanese/Chinese/Korean" if cjk else ""))

    def browse(self, variable: tk.StringVar):
        folder = filedialog.askdirectory(parent=self, initialdir=variable.get() or None)
        if folder:
            variable.set(str(Path(folder)))

    @staticmethod
    def open_folder(folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        core.open_in_explorer(folder)

    def fonts(self):
        FontsDialog(self).show()
        self.grab_set()
        self.update_fonts_label(*core.installed_fonts())

    def accept(self):
        discord_id = self.discord.get().strip()
        if discord_id and not discord_id.isdigit():
            messagebox.showerror("Discord status", "The Application ID is a number, like 1234567890123456789.", parent=self)
            return
        settings = self.settings
        changed_theme = settings.theme != self.theme.get()
        changed_folders = (settings.start_menu_folder, settings.desktop_folder) != (self.start_menu.get(), self.desktop.get())
        settings.theme = self.theme.get()
        settings.auto_backup = self.auto_backup.get()
        settings.notify_updates = self.notify.get()
        settings.fps_overlay = self.fps_overlay.get()
        settings.screenshot_key = self.screenshot_key.get()
        settings.screenshot_hotkey = self.screenshot_hotkey.get()
        settings.discord_app_id = discord_id
        settings.start_menu_folder = self.start_menu.get()
        settings.desktop_folder = self.desktop.get()
        settings.source_dir = self.source.get().strip()
        settings.toolchain_dir = self.toolchain.get().strip()
        settings.save()
        self.result = {"theme": changed_theme, "folders": changed_folders}
        self.destroy()


class CaptureDialog(Dialog):
    """Waits for one key press, mouse click or wheel turn and returns it as an anyps5-input.ini binding."""

    def __init__(self, master: tk.Misc, action: str):
        super().__init__(master, f"Bind {action}")
        area = tk.Label(self.body, text=f"Press a key, click a mouse button,\nor turn the wheel here for {action}.",
                        width=42, height=6, relief="flat", bg=palette["surface2"], fg=palette["text"], font=(FONT, 11))
        area.pack()
        self.buttons(("Cancel", self.destroy, False)).pack(anchor="e", pady=(12, 0))
        self.unbind("<Escape>")  # Escape is a bindable key
        self.bind("<KeyPress>", self.on_key)
        area.bind("<ButtonPress>", self.on_button)
        area.bind("<MouseWheel>", lambda event: self.finish("WHEEL:Up" if event.delta > 0 else "WHEEL:Down"))

    def on_key(self, event):
        name = sdl_key_name(event.keysym)
        if name:
            self.finish(f"KEY:{name}")

    def on_button(self, event):
        names = {1: "Left", 2: "Middle", 3: "Right"}
        if os.name == "nt":
            names.update({4: "X1", 5: "X2"})
        if event.num in names:
            self.finish(f"MOUSE:{names[event.num]}")

    def finish(self, binding: str):
        self.result = binding
        self.destroy()


class ControlsDialog(Dialog):
    """Keyboard and mouse layout for all games, or for one game (its own anyps5-input.ini beside its executable)."""

    def __init__(self, master: tk.Misc, game: core.Game | None = None):
        super().__init__(master, f"Controls: {game.title}" if game else "Keyboard and mouse controls")
        self.game = game
        self.path = core.game_input_file(game) if game else core.INPUT_FILE
        own = game is not None and core.has_own_controls(game)
        source = self.path if game is None or own else core.INPUT_FILE
        self.bindings = {action: list(items) for action, items in core.read_input_file(source).items()}
        if game is None:
            scope = ("These settings change the keyboard and mouse for every game; a game can have its own layout\n"
                     "(Game settings > Controls). An action left on built-in keeps its defaults.")
        elif own:
            scope = f"{game.title} uses its own layout. An action left on built-in keeps its defaults."
        else:
            scope = (f"{game.title} uses the layout shared by all games. Saving here gives it its own layout,\n"
                     "starting from the shared one.")
        ttk.Label(self.body, justify="left", style="Muted.TLabel",
                  text="Game controllers (Xbox, DualSense, DualShock) work automatically; this is the keyboard and mouse.\n"
                       + scope).pack(anchor="w")
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True, pady=(12, 0))
        self.tree = ttk.Treeview(frame, columns=("custom", "builtin"), show="tree headings", height=14,
                                 selectmode="browse", style="Compact.Treeview")
        self.tree.heading("#0", text="PS5 action", anchor="w")
        self.tree.heading("custom", text="Your binding", anchor="w")
        self.tree.heading("builtin", text="Built-in", anchor="w")
        self.tree.column("#0", width=150, stretch=False)
        self.tree.column("custom", width=230)
        self.tree.column("builtin", width=230)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for action, builtin in core.INPUT_ACTIONS.items():
            self.tree.insert("", "end", iid=action, text=action, values=("", builtin or "(none)"))
        self.refresh()
        edit = ttk.Frame(self.body)
        edit.pack(fill="x", pady=(10, 0))
        ttk.Button(edit, text="Set…", command=lambda: self.capture(replace=True)).pack(side="left")
        ttk.Button(edit, text="Add another…", command=lambda: self.capture(replace=False)).pack(side="left", padx=(6, 0))
        ttk.Button(edit, text="Use built-in", command=self.clear).pack(side="left", padx=(6, 0))
        ttk.Button(edit, text="Reset all", command=self.reset).pack(side="left", padx=(18, 0))
        ttk.Button(edit, text="Test controller…", command=lambda: (ControllerDialog(self).show(), self.grab_set())).pack(side="right")
        bottom = ttk.Frame(self.body)
        bottom.pack(fill="x", pady=(18, 0))
        if game is not None and own:
            ttk.Button(bottom, text="Use the shared layout again", command=self.use_shared).pack(side="left")
        self.buttons(("Save", self.accept, True), ("Cancel", self.destroy, False)).pack(in_=bottom, side="right")
        self.tree.bind("<Double-1>", lambda _event: self.capture(replace=True))
        self.tree.selection_set("Cross")

    def refresh(self):
        for action, builtin in core.INPUT_ACTIONS.items():
            custom = ", ".join(self.bindings.get(action, []))
            self.tree.item(action, values=(custom or "built-in", builtin or "(none)"), tags=("custom",) if custom else ())
        self.tree.tag_configure("custom", foreground=palette["running"])

    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def capture(self, replace: bool):
        action = self.selected()
        if not action:
            return
        binding = CaptureDialog(self, action).show()
        self.grab_set()
        if not binding:
            return
        items = [] if replace else self.bindings.get(action, [])
        if binding not in items:
            items.append(binding)
        self.bindings[action] = items
        self.refresh()

    def clear(self):
        action = self.selected()
        if action:
            self.bindings.pop(action, None)
            self.refresh()

    def reset(self):
        self.bindings = {}
        self.refresh()

    def accept(self):
        for action, items in self.bindings.items():
            for binding in items:
                error = core.validate_binding(binding)
                if error:
                    messagebox.showerror("Controls", f"{action}: {error}", parent=self)
                    return
        if self.game is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        core.write_input_file(self.bindings, self.path, keep_empty=self.game is not None)
        self.result = {"path": self.path, "game": self.game}
        self.destroy()

    def use_shared(self):
        self.path.unlink(missing_ok=True)
        self.result = {"path": None, "game": self.game}
        self.destroy()


class WhatsNewDialog(Dialog):
    """Upstream AnyPS5 changes not in the local build yet, grouped, with the ones that touch the user's games first."""

    ORDER = ("New", "Fixed", "Faster", "Other")

    def __init__(self, master, repo: Path, games: list[core.Game]):
        super().__init__(master, "What's new in AnyPS5", resizable=True)
        self.repo, self.games = repo, games
        self.state: dict = {}
        ttk.Label(self.body, text="Changes since your build", style="Heading.TLabel").pack(anchor="w")
        self.summary = ttk.Label(self.body, text="Looking for new changes…", style="Small.TLabel", wraplength=720, justify="left")
        self.summary.pack(anchor="w", pady=(2, 8))
        self.mine = ttk.Label(self.body, text="", wraplength=720, justify="left")
        self.mine.pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("area", "pr"), show="tree headings", height=14, style="Compact.Treeview")
        for column, heading, width in (("#0", "Change", 520), ("area", "Part", 120), ("pr", "Pull request", 90)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("group", font=(FONT, 10, "bold"))
        self.tree.tag_configure("mine", foreground=palette["ok"])
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(12, 0))
        self.update_button = ttk.Button(row, text="Update now", style="Accent.TButton", command=self.accept)
        self.update_button.pack(side="right")
        self.update_button.state(["disabled"])
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right", padx=(0, 6))
        threading.Thread(target=self._load, daemon=True).start()
        self.after(150, self.watch)

    def _load(self):
        try:
            changes = core.pending_changes(self.repo)
            mine = core.changes_for_games(changes, self.games)
            self.state.update(changes=changes, mine=mine)  # both at once: the window polls this from another thread
        except (core.LauncherError, OSError) as error:
            self.state["error"] = str(error)

    def watch(self):
        if not self.winfo_exists():
            return
        if not self.state:
            self.after(150, self.watch)
            return
        if "error" in self.state:
            self.summary.configure(text=f"Could not read the changes: {self.state['error']}")
            return
        changes, mine = self.state["changes"], self.state["mine"]
        if not changes:
            self.summary.configure(text="Your AnyPS5 build is up to date. There is nothing new yet.")
            return
        counts = {kind: sum(1 for change in changes if change.kind == kind) for kind in self.ORDER}
        self.summary.configure(text=f"{len(changes)} changes: " + ", ".join(f"{count} {kind.lower()}"
                                                                         for kind, count in counts.items() if count)
                               + ". Updating rebuilds AnyPS5 with them (a minute or several).")
        flagged = {change.commit for entries in mine.values() for change, _ in entries}
        lines = []
        for game in self.games:
            for change, reason in mine.get(game.id, []):
                lines.append(f"• {game.title}: “{change.title}” ({reason})")
        self.mine.configure(text=("Likely to help your games:\n" + "\n".join(lines[:8])) if lines else
                            "None of these changes names a function your games are missing; they may still help.",
                            foreground=palette["ok"] if lines else palette["muted"])
        for kind in self.ORDER:
            group = [change for change in changes if change.kind == kind]
            if not group:
                continue
            parent = self.tree.insert("", "end", text=f"{kind} ({len(group)})", open=True, tags=("group",))
            for change in group:
                self.tree.insert(parent, "end", text=change.title, values=(change.area, f"#{change.pr}" if change.pr else change.commit),
                                 tags=("mine",) if change.commit in flagged else ())
        self.update_button.state(["!disabled"])

    def accept(self):
        self.result = True
        self.destroy()


class ExportDialog(Dialog):
    def __init__(self, master, library: core.Library):
        super().__init__(master, "Move to another PC: export")
        backups = sum(len(core.list_backups(game)) for game in library.games)
        shots = [shot for game in library.games for shot in core.list_screenshots(game)]
        ttk.Label(self.body, text="Export the launcher to one file", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.body, style="Small.TLabel", wraplength=480, justify="left", text=(
            f"Includes {len(library.games)} games with their settings, playtime, tags, notes, compatibility history and "
            f"controls, plus the newest saves and {backups} save backups. The games themselves and their converted "
            "copies stay out: on the other PC the launcher finds the game folders and converts them again, then puts "
            "the saves back.")).pack(anchor="w", pady=(2, 10))
        self.screenshots = tk.BooleanVar(value=False)
        size = core.human_size(sum(shot.stat().st_size for shot in shots))
        check = ttk.Checkbutton(self.body, variable=self.screenshots, text=f"Include screenshots ({len(shots)}, {size})")
        check.pack(anchor="w")
        if not shots:
            check.state(["disabled"])
        self.buttons(("Export…", self.accept, True), ("Cancel", self.destroy, False)).pack(anchor="e", pady=(16, 0))

    def accept(self):
        name = f"AnyPS5 launcher export {time.strftime('%Y-%m-%d')}.zip"
        target = filedialog.asksaveasfilename(parent=self, defaultextension=".zip", initialfile=name,
                                              filetypes=[("Zip archive", "*.zip")])
        if target:
            self.result = {"target": Path(target), "screenshots": self.screenshots.get()}
            self.destroy()


class ImportDialog(Dialog):
    def __init__(self, master, bundle_path: Path, bundle: dict, library: core.Library):
        super().__init__(master, "Move to another PC: import", resizable=True)
        self.path, self.bundle, self.library = bundle_path, bundle, library
        self.relocations: dict[str, Path] = {}
        self.search: dict = {}
        known = {game.id for game in library.games}
        self.games = [game for game in bundle["games"] if game.id not in known]
        manifest = bundle["manifest"]
        ttk.Label(self.body, text=f"Import from {bundle_path.name}", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.body, style="Small.TLabel", text=(
            f"Exported {manifest.get('exported', '?')} with {len(bundle['games'])} games; {len(bundle['games']) - len(self.games)} "
            "are already in this library.")).pack(anchor="w", pady=(2, 8))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("status", "folder"), show="tree headings", height=10, style="Compact.Treeview")
        for column, heading, width in (("#0", "Game", 230), ("status", "Game folder on this PC", 170), ("folder", "Folder", 340)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("missing", foreground=palette["warn"])
        self.note = ttk.Label(self.body, style="Small.TLabel", wraplength=740, justify="left", text=(
            "Games whose folder is elsewhere on this PC can be found by title ID. Games left missing are still imported; "
            "point them at their folder later with More > Change game folder."))
        self.note.pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(12, 0))
        self.find_button = ttk.Button(row, text="Look for missing games in a folder…", command=self.find)
        self.find_button.pack(side="left")
        self.import_button = ttk.Button(row, text=f"Import {len(self.games)} games", style="Accent.TButton", command=self.accept)
        self.import_button.pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))
        if not self.games:
            self.import_button.state(["disabled"])
        self.refresh()

    def folder_of(self, game: core.Game) -> Path:
        return self.relocations.get(game.id, Path(game.source_dir))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        missing = 0
        for game in self.games:
            folder = self.folder_of(game)
            present = folder.is_dir()
            missing += not present
            status = ("Found by title ID" if game.id in self.relocations else "Same place") if present else "Not found yet"
            self.tree.insert("", "end", text=game.title, values=(status, str(folder)), tags=() if present else ("missing",))
        self.find_button.state(["!disabled"] if missing else ["disabled"])

    def find(self):
        folder = filedialog.askdirectory(parent=self, title="Folder or drive where the games are")
        if not folder:
            return
        self.find_button.state(["disabled"])
        self.note.configure(text="Looking for the games…")
        state = self.search = {}

        def work():
            state["found"] = core.find_moved_games(self.games, Path(folder))

        threading.Thread(target=work, daemon=True).start()
        self.after(150, self.watch)

    def watch(self):
        if not self.winfo_exists():
            return
        if "found" not in self.search:
            self.after(150, self.watch)
            return
        self.relocations.update(self.search["found"])
        self.note.configure(text=f"Found {len(self.search['found'])} of the missing games.")
        self.refresh()

    def accept(self):
        self.result = dict(self.relocations)
        self.destroy()


class FontsDialog(Dialog):
    """Downloads the openly licensed Noto fonts AnyPS5 substitutes for the console's system fonts."""

    def __init__(self, master: tk.Misc):
        super().__init__(master, "Free fonts")
        ttk.Label(self.body, text="Fonts for games that use the console's system fonts", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.body, justify="left", style="Small.TLabel", wraplength=480, text=(
            "AnyPS5 uses Google's Noto fonts (SIL Open Font License) in place of the console's. They are downloaded "
            f"one by one from Google's GitHub repositories into {core.FONTS_DIR}.")).pack(anchor="w", pady=(2, 10))
        basic, cjk = core.installed_fonts()
        ttk.Label(self.body, text=f"Latin, Greek, Cyrillic, Vietnamese, monospace and Thai, about 7.5 MB "
                                  f"({basic} of {len(core.FONT_FILES)} installed)").pack(anchor="w")
        self.cjk = tk.BooleanVar(value=cjk > 0)
        ttk.Checkbutton(self.body, variable=self.cjk, text=f"Also Japanese, Chinese and Korean, about 76 MB "
                                                           f"({cjk} of {len(core.CJK_FONT_FILES)} installed)").pack(anchor="w", pady=(6, 0))
        self.progress = ttk.Label(self.body, text="", style="Small.TLabel")
        self.progress.pack(anchor="w", pady=(12, 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(14, 0))
        self.download_button = ttk.Button(row, text="Download", style="Accent.TButton", command=self.start)
        self.download_button.pack(side="right")
        self.close_button = ttk.Button(row, text="Close", command=self.stop)
        self.close_button.pack(side="right", padx=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.stop)
        self.cancel = threading.Event()
        self.state_text: str | None = None
        self.finished: str | None = None

    def start(self):
        self.download_button.state(["disabled"])
        include_cjk = self.cjk.get()

        def work():
            try:
                fetched = core.download_fonts(include_cjk, lambda text, _tag: setattr(self, "state_text", f"Downloading {text}"),
                                              self.cancel)
                self.finished = f"Done: {fetched} font files downloaded." if fetched else "Everything was already installed."
            except (core.LauncherError, OSError) as error:
                self.finished = f"Stopped: {error}"

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.watch)

    def watch(self):
        if not self.winfo_exists():
            return
        if self.finished is not None:
            self.progress.configure(text=self.finished)
            self.download_button.state(["!disabled"])
            return
        self.progress.configure(text=self.state_text or "Starting…")
        self.after(150, self.watch)

    def stop(self):
        self.cancel.set()
        self.destroy()


class ScanDialog(Dialog):
    """Finds games under a folder or drive and returns the ticked ones with their output folders."""

    STATUS = {"new": "New", "library": "Already in the library", "error": "Cannot add"}

    def __init__(self, master, library: core.Library):
        super().__init__(master, "Scan a folder for games", resizable=True)
        self.library = library
        self.results: list[core.ScanResult] = []
        self.checked: set[str] = set()
        self.scan_state: dict = {}
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Label(top, text="Look in").pack(side="left")
        self.folder = tk.StringVar()
        ttk.Entry(top, textvariable=self.folder, width=56).pack(side="left", padx=(8, 6), fill="x", expand=True)
        ttk.Button(top, text="Browse…", command=self.browse).pack(side="left")
        self.scan_button = ttk.Button(top, text="Scan", style="Accent.TButton", command=self.scan)
        self.scan_button.pack(side="left", padx=(6, 0))
        self.status = ttk.Label(self.body, text="Pick a folder or a whole drive. Converted copies are skipped.", style="Small.TLabel")
        self.status.pack(anchor="w", pady=(8, 6))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("id", "version", "status", "folder"), show="tree headings", height=12,
                                 selectmode="browse", style="Compact.Treeview")
        for column, heading, width in (("#0", "Game", 240), ("id", "Title ID", 90), ("version", "Version", 90),
                                       ("status", "Status", 170), ("folder", "Folder", 300)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.toggle)
        self.tree.bind("<space>", lambda _event: self.toggle_selected())
        self.tree.tag_configure("disabled", foreground=palette["faint"])
        where = ttk.Frame(self.body)
        where.pack(fill="x", pady=(12, 0))
        self.placement = tk.StringVar(value="beside")
        ttk.Radiobutton(where, text="Put each converted game next to its folder (no extra space on the same drive)",
                        value="beside", variable=self.placement).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(where, text="Put them all in", value="folder", variable=self.placement).grid(row=1, column=0, sticky="w")
        self.target = tk.StringVar()
        ttk.Entry(where, textvariable=self.target, width=48).grid(row=1, column=1, sticky="we", padx=(6, 6))
        ttk.Button(where, text="Browse…", command=self.browse_target).grid(row=1, column=2)
        where.columnconfigure(1, weight=1)
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(14, 0))
        self.add_button = ttk.Button(row, text="Add 0 games", style="Accent.TButton", command=self.accept)
        self.add_button.pack(side="right")
        ttk.Button(row, text="Cancel", command=self.close).pack(side="right", padx=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.cancel = threading.Event()
        self.update_count()

    def browse(self):
        folder = filedialog.askdirectory(parent=self, title="Folder or drive to scan")
        if folder:
            self.folder.set(str(Path(folder)))
            self.scan()

    def browse_target(self):
        folder = filedialog.askdirectory(parent=self, title="Folder for the converted games")
        if folder:
            self.target.set(str(Path(folder)))
            self.placement.set("folder")

    def scan(self):
        root = Path(self.folder.get().strip())
        if not root.is_dir():
            messagebox.showerror("Scan", "Pick an existing folder or drive.", parent=self)
            return
        self.scan_button.state(["disabled"])
        self.tree.delete(*self.tree.get_children())
        self.results, self.checked = [], set()
        self.scan_state = {"current": "", "done": False}
        state = self.scan_state

        def work():
            try:
                state["results"] = core.scan_for_games(root, self.library, cancel=self.cancel,
                                                       progress=lambda folder: state.update(current=folder))
            except OSError as error:
                state["error"] = str(error)
            state["done"] = True

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.watch)

    def watch(self):
        if not self.winfo_exists():
            return
        state = self.scan_state
        if not state.get("done"):
            self.status.configure(text=f"Scanning… {state.get('current', '')}")
            self.after(150, self.watch)
            return
        self.scan_button.state(["!disabled"])
        if "error" in state:
            self.status.configure(text=f"Scan failed: {state['error']}")
            return
        self.results = state["results"]
        for index, result in enumerate(self.results):
            key = str(index)
            if result.info and not result.in_library:
                status, tags = self.STATUS["new"], ()
                self.checked.add(key)
            elif result.in_library:
                status, tags = self.STATUS["library"], ("disabled",)
            else:
                status, tags = f"{self.STATUS['error']}: {result.error.split('.')[0]}", ("disabled",)
            title = result.info.title if result.info else result.folder.name
            self.tree.insert("", "end", iid=key, text=title, tags=tags,
                             values=(result.info.title_id if result.info else "", result.info.version if result.info else "",
                                     status, str(result.folder)))
        self.refresh_marks()
        found = sum(1 for result in self.results if result.info)
        self.status.configure(text=f"Found {found} game{'s' if found != 1 else ''}"
                                   + (f" and {len(self.results) - found} folders that cannot be added" if len(self.results) > found else "")
                                   + ". Click a row to tick or untick it.")

    def refresh_marks(self):
        for key in self.tree.get_children():
            result = self.results[int(key)]
            addable = result.info is not None and not result.in_library
            mark = ("☑ " if key in self.checked else "☐ ") if addable else "   "
            self.tree.item(key, text=mark + (result.info.title if result.info else result.folder.name))
        self.update_count()

    def toggle(self, event):
        key = self.tree.identify_row(event.y)
        if key:
            self.tree.selection_set(key)
            self.flip(key)

    def toggle_selected(self):
        selection = self.tree.selection()
        if selection:
            self.flip(selection[0])

    def flip(self, key: str):
        result = self.results[int(key)]
        if result.info is None or result.in_library:
            return
        self.checked.symmetric_difference_update({key})
        self.refresh_marks()

    def update_count(self):
        count = len(self.checked)
        self.add_button.configure(text=f"Add and convert {count} game{'s' if count != 1 else ''}")
        self.add_button.state(["!disabled"] if count else ["disabled"])

    def accept(self):
        target = Path(self.target.get().strip()) if self.placement.get() == "folder" else None
        if self.placement.get() == "folder" and (target is None or not str(target).strip()):
            messagebox.showerror("Scan", "Pick the folder for the converted games.", parent=self)
            return
        chosen = []
        for key in sorted(self.checked, key=int):
            info = self.results[int(key)].info
            output = (target / f"{info.folder.name}-anyps5") if target else core.default_output_dir(info.folder)
            chosen.append((info, output))
        self.result = chosen
        self.close()

    def close(self):
        self.cancel.set()
        self.destroy()


class StorageDialog(Dialog):
    """Space each converted game takes, with clean-ups for the parts that can be rebuilt or are spare."""

    COLUMNS = (("total", "Total", 90), ("game_files", "Game files", 90), ("libraries", "Libraries", 90),
               ("shader_cache", "Shader cache", 100), ("saves", "Saves", 80), ("backups", "Backups", 80))

    def __init__(self, master, library: core.Library, is_running, log):
        super().__init__(master, "Storage", resizable=True)
        self.library, self.is_running, self.log = library, is_running, log
        self.reports: dict[str, dict] = {}
        ttk.Label(self.body, text="Space used by converted games", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.body, style="Small.TLabel", wraplength=700, justify="left", text=(
            "Game files linked from the original folder take no extra space and are not counted. Shader caches are "
            "rebuilt as you play (the first run is a little slower); libraries come back with the next conversion.")).pack(
            anchor="w", pady=(2, 10))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[key for key, _, _ in self.COLUMNS], show="tree headings", height=10,
                                 selectmode="browse", style="Compact.Treeview")
        self.tree.heading("#0", text="Game", anchor="w")
        self.tree.column("#0", width=230, anchor="w")
        for key, heading, width in self.COLUMNS:
            self.tree.heading(key, text=heading, anchor="e")
            self.tree.column(key, width=width, anchor="e")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.total = ttk.Label(self.body, text="Measuring…", style="Status.TLabel")
        self.total.pack(anchor="w", pady=(8, 0))
        actions = ttk.Frame(self.body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Clear shader cache", command=lambda: self.clear(selected_only=True)).pack(side="left")
        ttk.Button(actions, text="Clear all shader caches", command=lambda: self.clear(selected_only=False)).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Keep 3 newest automatic backups", command=self.trim).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Open folder", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Close", command=self.destroy).pack(side="right")
        for game in sorted(library.games, key=lambda item: item.title.lower()):
            self.tree.insert("", "end", iid=game.id, text=game.title, values=["…"] * len(self.COLUMNS))
        self.measure()

    def measure(self):
        games = list(self.library.games)
        self.pending = {game.id for game in games}

        def work():
            for game in games:
                try:
                    self.reports[game.id] = core.storage_report(game)
                except OSError:
                    self.reports[game.id] = {}
                self.pending.discard(game.id)

        threading.Thread(target=work, daemon=True).start()
        self.after(150, self.watch)

    def watch(self):
        if not self.winfo_exists():
            return
        for game_id, report in list(self.reports.items()):
            if self.tree.exists(game_id) and report:
                self.tree.item(game_id, values=[core.human_size(report.get(key, 0)) for key, _, _ in self.COLUMNS])
        total = sum(report.get("total", 0) for report in self.reports.values())
        self.total.configure(text=f"Total: {core.human_size(total)}" + ("  (still measuring…)" if self.pending else ""))
        if self.pending:
            self.after(250, self.watch)

    def selected_games(self, selected_only: bool) -> list[core.Game]:
        if selected_only:
            selection = self.tree.selection()
            game = self.library.get(selection[0]) if selection else None
            return [game] if game else []
        return list(self.library.games)

    def clear(self, selected_only: bool):
        games = self.selected_games(selected_only)
        if not games:
            messagebox.showinfo("Storage", "Select a game first.", parent=self)
            return
        freed, skipped = 0, []
        for game in games:
            if self.is_running(game):
                skipped.append(game.title)
                continue
            freed += core.clear_shader_cache(game)
        self.log(f"Shader caches cleared: {core.human_size(freed)} freed"
                 + (f" (skipped {', '.join(skipped)}: running)" if skipped else ""), "ok")
        self.measure()

    def trim(self):
        freed = sum(core.prune_auto_backups(game, keep=3) for game in self.library.games)
        self.log(f"Older automatic save backups removed: {core.human_size(freed)} freed (manual backups kept)", "ok")
        self.measure()

    def open_folder(self):
        games = self.selected_games(True)
        if games and games[0].output.is_dir():
            core.open_in_explorer(games[0].output)


class ControllerDialog(Dialog):
    """Live view of an Xbox-style (XInput) controller, labelled with the PS5 buttons AnyPS5 maps it to."""

    FACE = {"Triangle": (0, -1, "△"), "Circle": (1, 0, "○"), "Cross": (0, 1, "✕"), "Square": (-1, 0, "□")}
    DPAD = {"Up": (0, -1), "Right": (1, 0), "Down": (0, 1), "Left": (-1, 0)}

    def __init__(self, master: tk.Misc):
        super().__init__(master, "Controller test")
        self.pad = core.XInput()
        self.index: int | None = None
        self.playstation: list[str] | None = None
        ttk.Label(self.body, text="Press buttons and move the sticks. Labels are the PS5 buttons games receive.",
                  style="Muted.TLabel").pack(anchor="w")
        self.status = ttk.Label(self.body, text="", style="Heading.TLabel")
        self.status.pack(anchor="w", pady=(8, 6))
        self.canvas = tk.Canvas(self.body, width=520, height=290, highlightthickness=0, bd=0, bg=palette["surface"])
        self.canvas.pack()
        self.ps_label = ttk.Label(self.body, text="Looking for PlayStation controllers…", style="Small.TLabel",
                                  wraplength=520, justify="left")
        self.ps_label.pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(14, 0))
        self.vibrate_button = ttk.Button(row, text="Vibrate", command=self.vibrate)
        self.vibrate_button.pack(side="left")
        ttk.Button(row, text="Close", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._draw()
        threading.Thread(target=self._find_playstation, daemon=True).start()
        self.after(30, self.tick)

    def _find_playstation(self):
        self.playstation = core.playstation_controllers()

    def _draw(self):
        p, c = palette, self.canvas
        for side, x in (("L", 70), ("R", 360)):
            c.create_rectangle(x, 22, x + 90, 36, outline=p["border"], fill=p["surface2"], tags=f"{side}2frame")
            c.create_rectangle(x, 22, x, 36, outline="", fill=p["accent"], tags=f"{side}2bar")
            c.create_text(x + 45, 48, text=f"{side}2", fill=p["muted"], font=(FONT, 8))
            c.create_rectangle(x, 60, x + 90, 80, outline=p["border"], fill=p["surface2"], tags=f"{side}1")
            c.create_text(x + 45, 70, text=f"{side}1", fill=p["text"], font=(FONT, 9))
        for name, (dx, dy) in self.DPAD.items():
            x, y = 115 + dx * 26, 150 + dy * 26
            c.create_rectangle(x - 12, y - 12, x + 12, y + 12, outline=p["border"], fill=p["surface2"], tags=name)
        for name, (dx, dy, symbol) in self.FACE.items():
            x, y = 405 + dx * 30, 150 + dy * 30
            c.create_oval(x - 15, y - 15, x + 15, y + 15, outline=p["border"], fill=p["surface2"], tags=name)
            c.create_text(x, y, text=symbol, fill=p["text"], font=(FONT, 11))
        for name, x, text in (("TouchPad", 215, "Touchpad"), ("Options", 305, "Options")):
            c.create_rectangle(x - 38, 110, x + 38, 130, outline=p["border"], fill=p["surface2"], tags=name)
            c.create_text(x, 120, text=text, fill=p["text"], font=(FONT, 8))
        for stick, x in (("L", 190), ("R", 330)):
            c.create_oval(x - 36, 220 - 36, x + 36, 220 + 36, outline=p["border"], fill=p["surface2"], tags=f"{stick}3")
            c.create_oval(x - 9, 211, x + 9, 229, outline="", fill=p["muted"], tags=f"{stick}dot")
            c.create_text(x, 270, text=f"{stick}3 (press the stick)", fill=p["muted"], font=(FONT, 8))

    def tick(self):
        if not self.winfo_exists():
            return
        p, c = palette, self.canvas
        if self.playstation is not None:
            self.ps_label.configure(text=(
                "PlayStation controller found: " + ", ".join(self.playstation) + ". Games read it directly; this live view "
                "only shows Xbox-style (XInput) controllers." if self.playstation else
                "No PlayStation controller found. DualSense and DualShock 4 work in games over USB or Bluetooth."))
            self.playstation = None
        state = None
        if self.index is not None:
            state = self.pad.read(self.index)
        if state is None:
            connected = self.pad.connected()
            self.index = connected[0] if connected else None
            state = self.pad.read(self.index) if self.index is not None else None
        if not self.pad.available:
            self.status.configure(text="XInput is not available on this system.")
        elif state is None:
            self.status.configure(text="No Xbox-style controller connected", foreground=p["muted"])
        else:
            self.status.configure(text=f"Controller {self.index + 1} connected", foreground=p["ok"])
        pressed = state["buttons"] if state else set()
        for name in [*self.FACE, *self.DPAD, "L1", "R1", "L3", "R3", "Options", "TouchPad"]:
            c.itemconfigure(name, fill=p["accent"] if name in pressed else p["surface2"])
        for side, x in (("L", 70), ("R", 360)):
            value = state[f"{side.lower()}2"] if state else 0
            c.coords(f"{side}2bar", x, 22, x + 90 * value, 36)
        for stick, x in (("L", 190), ("R", 330)):
            dx = state[f"{stick.lower()}x"] if state else 0
            dy = state[f"{stick.lower()}y"] if state else 0
            cx, cy = x + dx * 27, 220 - dy * 27
            c.coords(f"{stick}dot", cx - 9, cy - 9, cx + 9, cy + 9)
            c.itemconfigure(f"{stick}dot", fill=p["accent"] if state and (abs(dx) > 0.15 or abs(dy) > 0.15) else p["muted"])
        self.vibrate_button.state(["!disabled"] if state else ["disabled"])
        self.after(30, self.tick)

    def vibrate(self):
        if self.index is not None and self.pad.vibrate(self.index, 0.7, 0.7):
            self.after(600, lambda: self.pad.vibrate(self.index, 0, 0) if self.index is not None else None)

    def close(self):
        if self.index is not None:
            self.pad.vibrate(self.index, 0, 0)
        self.destroy()


class SavesDialog(Dialog):
    def __init__(self, master, game: core.Game, is_running, log):
        super().__init__(master, f"Saves: {game.title}", resizable=True)
        self.game, self.is_running, self.log = game, is_running, log
        ttk.Label(self.body, text=f"Saves for {game.title}", style="Heading.TLabel").pack(anchor="w")
        self.info = ttk.Label(self.body, text="", style="Small.TLabel", justify="left", wraplength=520)
        self.info.pack(anchor="w", pady=(2, 10))
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("kind", "size"), show="tree headings", height=10, selectmode="browse",
                                 style="Compact.Treeview")
        for column, heading, width in (("#0", "Backed up", 170), ("kind", "Kind", 190), ("size", "Size", 90)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        actions = ttk.Frame(self.body)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Back up now", style="Accent.TButton", command=self.backup_now).pack(side="left")
        for text, command in (("Restore", self.restore), ("Export…", self.export), ("Import…", self.import_backup),
                              ("Delete", self.delete)):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=(6, 0))
        folders = ttk.Frame(self.body)
        folders.pack(fill="x", pady=(8, 0))
        ttk.Button(folders, text="Open saves folder", command=self.open_saves).pack(side="left")
        ttk.Button(folders, text="Open backups folder", command=self.open_backups).pack(side="left", padx=(6, 0))
        ttk.Button(folders, text="Close", command=self.destroy).pack(side="right")
        self.tree.bind("<Double-1>", lambda _event: self.restore())
        self.refresh()

    def refresh(self):
        self.backups = {str(backup.path): backup for backup in core.list_backups(self.game)}
        self.tree.delete(*self.tree.get_children())
        for key, backup in self.backups.items():
            self.tree.insert("", "end", iid=key, text=backup.created, values=(BACKUP_KINDS.get(backup.kind, backup.kind),
                                                                              core.human_size(backup.size)))
        saves = core.saves_dir(self.game)
        files = [path for path in saves.rglob("*") if path.is_file()] if saves.is_dir() else []
        current = (f"{len(files)} files, {core.human_size(sum(path.stat().st_size for path in files))}"
                   if files else "no saves yet")
        self.info.configure(text=f"Current saves: {current}  ·  {saves}\n{len(self.backups)} backups")

    def selected(self) -> core.Backup | None:
        selection = self.tree.selection()
        return self.backups.get(selection[0]) if selection else None

    def backup_now(self):
        backup = core.backup_saves(self.game, "manual")
        if backup is None:
            messagebox.showinfo("Nothing to back up", "This game has no saves yet.", parent=self)
            return
        self.log(f"Saves of {self.game.title} backed up ({core.human_size(backup.size)})", "ok")
        self.refresh()

    def restore(self):
        backup = self.selected()
        if backup is None:
            return
        if self.is_running(self.game):
            messagebox.showerror("Game running", "Stop the game before restoring its saves.", parent=self)
            return
        if not messagebox.askyesno("Restore saves", f"Replace the current saves with the backup from {backup.created}?\n\n"
                                   "The current saves are backed up first, so you can undo this.", parent=self):
            return
        try:
            core.restore_backup(self.game, backup.path)
        except (core.LauncherError, OSError) as error:
            messagebox.showerror("Restore failed", str(error), parent=self)
            return
        self.log(f"Saves of {self.game.title} restored from {backup.created}", "ok")
        self.refresh()

    def export(self):
        backup = self.selected()
        if backup is None:
            backup = core.backup_saves(self.game, "manual")
            if backup is None:
                messagebox.showinfo("Nothing to export", "This game has no saves yet.", parent=self)
                return
            self.refresh()
        name = f"{core.safe_filename(self.game.title)} saves {backup.created[:10]}.zip"
        target = filedialog.asksaveasfilename(parent=self, defaultextension=".zip", initialfile=name,
                                              filetypes=[("Zip archive", "*.zip")])
        if target:
            shutil.copy2(backup.path, target)
            self.log(f"Saves of {self.game.title} exported to {target}", "ok")

    def import_backup(self):
        source = filedialog.askopenfilename(parent=self, filetypes=[("Zip archive", "*.zip")])
        if not source:
            return
        try:
            backup = core.import_backup(self.game, Path(source))
        except (core.LauncherError, OSError) as error:
            messagebox.showerror("Import failed", str(error), parent=self)
            return
        self.refresh()
        self.tree.selection_set(str(backup.path))
        self.restore()

    def delete(self):
        backup = self.selected()
        if backup and messagebox.askyesno("Delete backup", f"Delete the backup from {backup.created}?", parent=self):
            backup.path.unlink(missing_ok=True)
            self.refresh()

    def open_saves(self):
        folder = core.saves_dir(self.game)
        if folder.is_dir():
            core.open_in_explorer(folder)
        else:
            messagebox.showinfo("No saves yet", "The game has not saved anything yet.", parent=self)

    def open_backups(self):
        folder = core.backup_dir(self.game)
        folder.mkdir(parents=True, exist_ok=True)
        core.open_in_explorer(folder)


class HistoryDialog(Dialog):
    def __init__(self, master, game: core.Game):
        super().__init__(master, f"Compatibility history: {game.title}", resizable=True)
        ttk.Label(self.body, text="Missing functions per check", style="Heading.TLabel").pack(anchor="w")
        trend = core.history_trend(game) or "Each compatibility check adds a point; re-check after updating AnyPS5."
        ttk.Label(self.body, text=trend, style="Small.TLabel").pack(anchor="w", pady=(2, 8))
        card = tk.Frame(self.body, bg=palette["surface"], padx=10, pady=8)
        card.pack(fill="x")
        chart = HistoryChart(card, width=620, height=190)
        chart.pack()
        chart.draw(game.audit_history)
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True, pady=(12, 0))
        tree = ttk.Treeview(frame, columns=("install", "share", "absent", "stub", "libraries"), show="tree headings",
                            height=8, style="Compact.Treeview")
        for column, heading, width in (("#0", "Checked", 140), ("install", "AnyPS5", 150), ("share", "Implemented", 100),
                                       ("absent", "Missing", 80), ("stub", "Unfinished", 90), ("libraries", "Missing libraries", 120)):
            tree.heading(column, text=heading, anchor="w")
            tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for entry in reversed(game.audit_history):
            tree.insert("", "end", text=entry["at"], values=(entry.get("install") or "", f"{core.implemented_share(entry):.0f}%",
                                                             entry["absent"], entry["stub"], entry["missing_libraries"]))
        self.buttons(("Close", self.destroy, False)).pack(anchor="e", pady=(14, 0))
