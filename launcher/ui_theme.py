"""Light and dark palettes for the launcher, applied to ttk (on the clam theme) and to classic Tk widgets."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

FONT = "Segoe UI"
SEMIBOLD = "Segoe UI Semibold"
MONO = "Consolas"

LIGHT = {
    "bg": "#f4f5f7", "surface": "#ffffff", "surface2": "#eceef1", "hover": "#e3e6eb", "border": "#d5d9e0",
    "text": "#16181d", "muted": "#5b6270", "faint": "#8a909c",
    "accent": "#2a78d6", "accent_hover": "#256abf", "accent_text": "#ffffff", "selection": "#dbe8fa",
    "ok": "#1a7f37", "warn": "#9a6700", "error": "#cf222e", "running": "#2a78d6", "idle": "#5b6270", "hint": "#7a3ec8",
    # Chart roles (dataviz reference: series slot 1, hairline grid, baseline), validated against "surface".
    "series": "#2a78d6", "grid": "#e1e0d9", "baseline": "#c3c2b7",
    "banner": "#e6effb", "log": "#f8f9fa",
}
DARK = {
    "bg": "#141518", "surface": "#1c1e23", "surface2": "#262931", "hover": "#2e323b", "border": "#363a44",
    "text": "#eceef1", "muted": "#a3a9b4", "faint": "#7d8390",
    "accent": "#3987e5", "accent_hover": "#5598e7", "accent_text": "#ffffff", "selection": "#1d3a63",
    "ok": "#3fb950", "warn": "#d29922", "error": "#f85149", "running": "#58a6ff", "idle": "#a3a9b4", "hint": "#b392f0",
    "series": "#3987e5", "grid": "#2c2c2a", "baseline": "#383835",
    "banner": "#1a2b44", "log": "#111216",
}

palette: dict = dict(LIGHT, name="light")


def system_prefers_dark() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve(mode: str) -> str:
    return "dark" if mode == "dark" or (mode == "system" and system_prefers_dark()) else "light"


def title_bar(window: tk.Misc) -> None:
    """Matches the Windows title bar to the palette (Windows 10 20H1 and later)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if palette["name"] == "dark" else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-20H1 number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass


def apply(root: tk.Tk, mode: str) -> str:
    name = resolve(mode)
    palette.clear()
    palette.update(DARK if name == "dark" else LIGHT, name=name)
    p = palette
    base = (FONT, 10)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["surface"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["surface2"], focuscolor=p["accent"],
                    selectbackground=p["selection"], selectforeground=p["text"], insertcolor=p["text"],
                    arrowcolor=p["muted"], font=base)
    style.configure("TFrame", background=p["bg"])
    labels = {
        "TLabel": {}, "Muted.TLabel": {"foreground": p["muted"]}, "Small.TLabel": {"foreground": p["muted"], "font": (FONT, 9)},
        "Title.TLabel": {"font": (SEMIBOLD, 20)}, "Brand.TLabel": {"font": (SEMIBOLD, 15)},
        "Heading.TLabel": {"font": (SEMIBOLD, 11)}, "Status.TLabel": {"font": (SEMIBOLD, 10)},
        "Stat.TLabel": {"font": (SEMIBOLD, 13)}, "Empty.TLabel": {"foreground": p["muted"], "font": (FONT, 11)},
    }
    for style_name, options in labels.items():
        style.configure(style_name, background=p["bg"], **options)

    flat = {"background": p["surface2"], "foreground": p["text"], "bordercolor": p["border"], "lightcolor": p["surface2"],
            "darkcolor": p["surface2"], "padding": (12, 5), "relief": "flat", "focusthickness": 1}
    for style_name in ("TButton", "TMenubutton"):
        style.configure(style_name, **flat)
        style.map(style_name, background=[("disabled", p["bg"]), ("pressed", p["border"]), ("active", p["hover"])],
                  lightcolor=[("disabled", p["bg"]), ("pressed", p["border"]), ("active", p["hover"])],
                  darkcolor=[("disabled", p["bg"]), ("pressed", p["border"]), ("active", p["hover"])],
                  foreground=[("disabled", p["faint"])], arrowcolor=[("disabled", p["faint"])])
    accent = {"background": p["accent"], "foreground": p["accent_text"], "bordercolor": p["accent"],
              "lightcolor": p["accent"], "darkcolor": p["accent"]}
    style.configure("Accent.TButton", **accent, font=(SEMIBOLD, 10))
    style.configure("Accent.TMenubutton", **accent, font=(SEMIBOLD, 10), arrowcolor=p["accent_text"])
    style.configure("Play.TButton", **accent, font=(SEMIBOLD, 11), padding=(22, 7))
    style.configure("Toggle.TButton", padding=(10, 4))
    style.configure("ToggleOn.TButton", **accent, padding=(10, 4))
    for style_name in ("Accent.TButton", "Accent.TMenubutton", "Play.TButton", "ToggleOn.TButton"):
        hover = [("disabled", p["surface2"]), ("pressed", p["accent_hover"]), ("active", p["accent_hover"])]
        style.map(style_name, background=hover, lightcolor=hover, darkcolor=hover, bordercolor=hover,
                  foreground=[("disabled", p["faint"])])
    style.configure("Link.TButton", background=p["bg"], foreground=p["accent"], bordercolor=p["bg"], lightcolor=p["bg"],
                    darkcolor=p["bg"], padding=(2, 1), relief="flat")
    style.map("Link.TButton", background=[("active", p["bg"])], lightcolor=[("active", p["bg"])],
              darkcolor=[("active", p["bg"])], foreground=[("active", p["accent_hover"]), ("disabled", p["faint"])])

    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"], padding=(6, 4))
    style.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])])
    style.configure("Placeholder.TEntry", fieldbackground=p["surface"], foreground=p["faint"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"], padding=(6, 4))
    style.configure("TCombobox", fieldbackground=p["surface"], background=p["surface2"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], arrowsize=14, padding=(6, 4))
    style.map("TCombobox", fieldbackground=[("readonly", p["surface"]), ("disabled", p["bg"])],
              background=[("active", p["hover"]), ("readonly", p["surface2"])],
              foreground=[("disabled", p["faint"])], bordercolor=[("focus", p["accent"])],
              selectbackground=[("readonly", p["surface"])], selectforeground=[("readonly", p["text"])])
    for style_name in ("TCheckbutton", "TRadiobutton"):
        style.configure(style_name, background=p["bg"], foreground=p["text"], indicatorbackground=p["surface"],
                        indicatorforeground=p["accent_text"], upperbordercolor=p["border"], lowerbordercolor=p["border"],
                        indicatormargin=(0, 0, 8, 0))
        style.map(style_name, background=[("active", p["bg"])],
                  indicatorbackground=[("selected", p["accent"]), ("active", p["hover"])],
                  upperbordercolor=[("selected", p["accent"])], lowerbordercolor=[("selected", p["accent"])],
                  foreground=[("disabled", p["faint"])])

    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], rowheight=42)
    style.map("Treeview", background=[("selected", p["selection"])], foreground=[("selected", p["text"])])
    style.configure("Compact.Treeview", rowheight=26)
    style.configure("Treeview.Heading", background=p["surface2"], foreground=p["muted"], bordercolor=p["border"],
                    lightcolor=p["surface2"], darkcolor=p["surface2"], relief="flat", font=(SEMIBOLD, 9), padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", p["hover"])])
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=p["surface2"], troughcolor=p["bg"], bordercolor=p["bg"],
                        lightcolor=p["surface2"], darkcolor=p["surface2"], arrowcolor=p["muted"], gripcount=0)
        style.map(f"{orient}.TScrollbar", background=[("active", p["hover"])])
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                    tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], padding=(16, 6), font=(SEMIBOLD, 10))
    style.map("TNotebook.Tab", background=[("selected", p["bg"]), ("active", p["hover"])],
              foreground=[("selected", p["text"])], lightcolor=[("selected", p["accent"])])
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["muted"], font=(SEMIBOLD, 10))
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Sash", sashthickness=8, gripcount=0, background=p["bg"], lightcolor=p["bg"], bordercolor=p["bg"])
    style.configure("TProgressbar", background=p["accent"], troughcolor=p["surface2"], bordercolor=p["surface2"],
                    lightcolor=p["accent"], darkcolor=p["accent"])
    style.configure("TSeparator", background=p["border"])

    # Classic widgets created from now on (combobox drop-downs, menus) read the option database.
    for pattern, value in (("*TCombobox*Listbox.background", p["surface"]), ("*TCombobox*Listbox.foreground", p["text"]),
                           ("*TCombobox*Listbox.selectBackground", p["selection"]),
                           ("*TCombobox*Listbox.selectForeground", p["text"]), ("*TCombobox*Listbox.font", base),
                           ("*Menu.background", p["surface"]), ("*Menu.foreground", p["text"]),
                           ("*Menu.activeBackground", p["selection"]), ("*Menu.activeForeground", p["text"]),
                           ("*Menu.disabledForeground", p["faint"]), ("*Menu.font", base), ("*Menu.relief", "flat")):
        root.option_add(pattern, value)
    root.configure(background=p["bg"])
    restyle_classic(root)
    return name


def restyle_classic(widget: tk.Misc) -> None:
    """Recolours classic widgets that already exist; ttk widgets follow the style on their own."""
    p = palette
    for child in [widget, *_descendants(widget)]:
        try:
            if isinstance(child, (tk.Tk, tk.Toplevel)):
                child.configure(background=p["bg"])
                title_bar(child)
            elif isinstance(child, tk.Menu):
                child.configure(background=p["surface"], foreground=p["text"], activebackground=p["selection"],
                                activeforeground=p["text"], disabledforeground=p["faint"])
            elif isinstance(child, ttk.Combobox):
                popdown = child.tk.call("ttk::combobox::PopdownWindow", child)
                child.tk.call(f"{popdown}.f.l", "configure", "-background", p["surface"], "-foreground", p["text"],
                              "-selectbackground", p["selection"], "-selectforeground", p["text"])
        except tk.TclError:
            continue


def _descendants(widget: tk.Misc):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)
