"""Library views (cover grid and list), game artwork loading, and the compatibility history chart."""
from __future__ import annotations

import math
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

import ui_theme as theme
from ui_theme import FONT, SEMIBOLD, palette

TILE_WIDTH, TILE_HEIGHT, TILE_GAP = 172, 246, 14  # room for a two-line title
TILE_ICON = 128


# ---------------------------------------------------------------------------
# Artwork


def placeholder(size: int) -> tk.PhotoImage:
    image = tk.PhotoImage(width=size, height=size)
    image.put(palette["surface2"], to=(0, 0, size, size))
    inset = size // 3
    image.put(palette["border"], to=(inset, inset, size - inset, size - inset))
    return image


def load_icon(path: Path, size: int) -> tk.PhotoImage:
    try:
        image = tk.PhotoImage(file=str(path))
    except (tk.TclError, OSError):
        return placeholder(size)
    factor = max(1, round(image.width() / size))
    return image.subsample(factor) if factor > 1 else image


def load_banner(path: Path, width: int, height: int) -> Optional[tk.PhotoImage]:
    """The game's sce_sys/pic0.png scaled down to `width` and cropped around its middle to `height`."""
    try:
        image = tk.PhotoImage(file=str(path))
    except (tk.TclError, OSError):
        return None
    factor = max(1, math.ceil(image.width() / width))
    scaled = image.subsample(factor) if factor > 1 else image
    if scaled.height() <= height:
        return scaled
    top = (scaled.height() - height) // 2
    cropped = tk.PhotoImage(width=scaled.width(), height=height)
    cropped.tk.call(cropped, "copy", scaled, "-from", 0, top, scaled.width(), top + height, "-to", 0, 0)
    return cropped


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Library views. Items are dicts: id, title, title_id, status, level, detail, image (tile), small (list),
# played, last. Both views call on_select(id), on_activate(id) and on_context(id, event).


class GridView(tk.Frame):
    def __init__(self, master, on_select: Callable, on_activate: Callable, on_context: Callable):
        super().__init__(master, highlightthickness=0, bd=0)
        self.on_select, self.on_activate, self.on_context = on_select, on_activate, on_context
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, takefocus=1)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bd=0, highlightthickness=0)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<MouseWheel>", self._wheel)
        for key, step in (("<Left>", -1), ("<Right>", 1), ("<Up>", "up"), ("<Down>", "down")):
            self.canvas.bind(key, lambda _event, step=step: self._move(step))
        self.canvas.bind("<Return>", lambda _event: self.selected and self.on_activate(self.selected))
        self.items: list[dict] = []
        self.tiles: dict[str, tk.Frame] = {}
        self.selected: Optional[str] = None
        self.columns = 0
        self.message = ""
        self.restyle()

    def restyle(self):
        p = palette
        for widget in (self, self.canvas, self.inner):
            widget.configure(bg=p["bg"])
        self.set_items(self.items, self.message)

    def set_items(self, items: list[dict], message: str = ""):
        self.items, self.message = items, message
        for child in self.inner.winfo_children():
            child.destroy()
        self.tiles = {item["id"]: self._tile(item) for item in items}
        if not items and message:
            tk.Label(self.inner, text=message, bg=palette["bg"], fg=palette["muted"], font=(FONT, 11)).grid(
                row=0, column=0, padx=24, pady=40, sticky="w")
        self.columns = 0
        self._layout()
        if self.selected in self.tiles:
            self._paint(self.selected)

    def _tile(self, item: dict) -> tk.Frame:
        p = palette
        tile = tk.Frame(self.inner, bg=p["surface"], width=TILE_WIDTH, height=TILE_HEIGHT, highlightthickness=2,
                        highlightbackground=p["bg"], highlightcolor=p["bg"], cursor="hand2")
        tile.pack_propagate(False)
        tk.Label(tile, image=item["image"], bg=p["surface"], bd=0).pack(pady=(14, 8))
        tk.Label(tile, text=shorten(item["title"], 40), bg=p["surface"], fg=p["text"], font=(SEMIBOLD, 10),
                 wraplength=TILE_WIDTH - 24, justify="center").pack(padx=10)
        tk.Label(tile, text="● " + item["status"], bg=p["surface"], fg=p[item["level"]], font=(FONT, 9)).pack(pady=(3, 0))
        tk.Label(tile, text=item["detail"], bg=p["surface"], fg=p["muted"], font=(FONT, 8)).pack()
        for widget in (tile, *tile.winfo_children()):
            widget.bind("<Button-1>", lambda _event, key=item["id"]: self._clicked(key))
            widget.bind("<Double-Button-1>", lambda _event, key=item["id"]: self.on_activate(key))
            widget.bind("<Button-3>", lambda event, key=item["id"]: (self._clicked(key), self.on_context(key, event)))
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<Enter>", lambda _event, key=item["id"]: self._hover(key, True))
            widget.bind("<Leave>", lambda event, key=item["id"]: self._leave(key, event))
        return tile

    def _layout(self):
        width = self.canvas.winfo_width()
        columns = max(1, (width - TILE_GAP) // (TILE_WIDTH + TILE_GAP)) if width > 1 else 3
        if columns == self.columns and self.items:
            return
        self.columns = columns
        for index, item in enumerate(self.items):
            self.tiles[item["id"]].grid(row=index // columns, column=index % columns,
                                        padx=(TILE_GAP, 0), pady=(TILE_GAP, 0), sticky="nw")

    def _resized(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)
        self._layout()

    def _wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _clicked(self, key: str):
        self.canvas.focus_set()
        self.select(key)
        self.on_select(key)

    def _paint(self, key: str, hover: bool = False):
        tile = self.tiles.get(key)
        if tile is None:
            return
        color = palette["accent"] if key == self.selected else (palette["border"] if hover else palette["bg"])
        tile.configure(highlightbackground=color, highlightcolor=color)

    def _hover(self, key: str, inside: bool):
        if key != self.selected:
            self._paint(key, inside)

    def _leave(self, key: str, event):
        tile = self.tiles.get(key)
        under = self.winfo_containing(event.x_root, event.y_root)
        if tile is not None and under is not None and (under == tile or str(under).startswith(str(tile) + ".")):
            return  # moved onto one of the tile's own labels
        self._hover(key, False)

    def select(self, key: Optional[str], scroll: bool = True):
        previous, self.selected = self.selected, key
        if previous:
            self._paint(previous)
        if key in self.tiles:
            self._paint(key)
            if scroll:
                self.after_idle(lambda: self._reveal(key))

    def _reveal(self, key: str):
        tile = self.tiles.get(key)
        if tile is None or not self.inner.winfo_height():
            return
        total = self.inner.winfo_height()
        top, bottom = tile.winfo_y(), tile.winfo_y() + tile.winfo_height()
        first, last = (fraction * total for fraction in self.canvas.yview())
        if top < first:
            self.canvas.yview_moveto(max(0, top - TILE_GAP) / total)
        elif bottom > last:
            self.canvas.yview_moveto((bottom + TILE_GAP - (last - first)) / total)

    def _move(self, step):
        if not self.items:
            return
        ids = [item["id"] for item in self.items]
        index = ids.index(self.selected) if self.selected in ids else 0
        delta = {"up": -self.columns, "down": self.columns}.get(step, step)
        target = ids[min(max(index + delta, 0), len(ids) - 1)]
        self.select(target)
        self.on_select(target)


class ListView(ttk.Frame):
    def __init__(self, master, on_select: Callable, on_activate: Callable, on_context: Callable):
        super().__init__(master)
        self.on_select, self.on_activate, self.on_context = on_select, on_activate, on_context
        self.tree = ttk.Treeview(self, columns=("id", "status", "played", "last"), show="tree headings", selectmode="browse")
        # Every column may shrink, down to its minimum, so a narrow pane never squeezes the title away.
        for column, heading, width, minimum in (("#0", "Game", 220, 150), ("id", "Title ID", 86, 70),
                                                ("status", "Status", 118, 96), ("played", "Played", 84, 64),
                                                ("last", "Last played", 92, 80)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, minwidth=minimum, stretch=True, anchor="w")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", lambda _event: self.selected and self.on_activate(self.selected))
        self.tree.bind("<Return>", lambda _event: self.selected and self.on_activate(self.selected))
        self.tree.bind("<Button-3>", self._context)
        self.selected: Optional[str] = None
        self.items: list[dict] = []
        self.quiet = False
        self.restyle()

    def restyle(self):
        for level in ("ok", "warn", "idle", "running", "error"):
            self.tree.tag_configure(level, foreground=palette[level])

    def set_items(self, items: list[dict], message: str = ""):
        self.items = items
        self.quiet = True
        self.tree.delete(*self.tree.get_children())
        for item in items:
            self.tree.insert("", "end", iid=item["id"], text="  " + item["title"], image=item["small"],
                             values=(item["title_id"], "● " + item["status"], item["played"], item["last"]),
                             tags=(item["level"],))
        if self.selected and self.tree.exists(self.selected):
            self.tree.selection_set(self.selected)
        self.quiet = False

    def select(self, key: Optional[str], scroll: bool = True):
        self.selected = key
        self.quiet = True
        if key and self.tree.exists(key):
            self.tree.selection_set(key)
            if scroll:
                self.tree.see(key)
        else:
            self.tree.selection_set(())
        self.quiet = False

    def _selected(self, _event):
        selection = self.tree.selection()
        if self.quiet or not selection:
            return
        self.selected = selection[0]
        self.on_select(self.selected)

    def _context(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.on_context(row, event)


class FpsOverlay(tk.Toplevel):
    """Small always-on-top frame-rate counter over the game. It never takes focus, and clicks pass through it.

    It is shown when Play is pressed, before the game window exists, so the game's window still gets the focus;
    afterwards it is only moved with SWP_NOACTIVATE.
    """

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.85)
        self.label = tk.Label(self, text="FPS  —", font=(SEMIBOLD, 11), bg="#0b0b0b", fg="#e6e8eb", padx=10, pady=4)
        self.label.pack()
        self.geometry("+16+16")
        self.deiconify()
        self.update_idletasks()
        self.hwnd = 0
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            # A private user32 with full argument types: handles are pointer-sized, and HWND_TOPMOST (-1) passed as a
            # plain int would be truncated to 32 bits on 64-bit Windows, making SetWindowPos fail.
            self.user32 = ctypes.WinDLL("user32")
            self.user32.GetParent.argtypes = [wintypes.HWND]
            self.user32.GetParent.restype = wintypes.HWND
            self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            self.user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
            self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
            self.hwnd = self.user32.GetParent(self.winfo_id()) or 0
            style = self.user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW (no taskbar button) | WS_EX_TRANSPARENT (click-through) | WS_EX_LAYERED
            self.user32.SetWindowLongW(self.hwnd, -20, style | 0x08000000 | 0x80 | 0x20 | 0x80000)

    def show(self, fps: float, origin: tuple[int, int]):
        self.label.configure(text=f"FPS  {fps:.0f}")
        x, y = origin[0] + 12, origin[1] + 12
        if self.hwnd:
            # HWND_TOPMOST, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW: re-asserted, as a fullscreen game is topmost too
            self.user32.SetWindowPos(self.hwnd, -1, x, y, 0, 0, 0x0001 | 0x0010 | 0x0040)
        else:
            self.geometry(f"+{x}+{y}")


class ScrollFrame(ttk.Frame):
    """Vertically scrolling container: put children in .body. The window routes wheel events here (see wheel)."""

    def __init__(self, master, padding=0):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=(0, 0, event.width, event.height)))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window, width=event.width))
        self.restyle()

    def restyle(self):
        self.canvas.configure(bg=palette["bg"])

    def contains(self, widget) -> bool:
        return str(widget).startswith(str(self))

    def wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def to_top(self):
        self.canvas.yview_moveto(0)


# ---------------------------------------------------------------------------
# Compatibility history chart: one series (missing functions per check), so no legend; the title names it.
# Marks follow the dataviz spec: 2px line, r=4 markers with a 2px surface ring, hairline baseline at zero,
# direct labels on the first and last value only, text in ink colours, and a hover readout per check.


class HistoryChart(tk.Canvas):
    def __init__(self, master, width: int = 340, height: int = 104):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0)
        self.size = (width, height)
        self.entries: list[dict] = []
        self.points: list[tuple[float, float, dict]] = []
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _event: self.delete("hover"))

    def draw(self, entries: list[dict]):
        p = palette
        self.entries = entries
        self.configure(bg=p["surface"])
        self.delete("all")
        width, height = self.size
        left, right, top, bottom = 16, 44, 20, 24
        values = [entry["absent"] for entry in entries]
        if not values:
            return
        ceiling = max(values) * 1.15 or 1
        count = len(values)
        xs = [left + (width - left - right) * (index / (count - 1) if count > 1 else 0.5) for index in range(count)]
        ys = [height - bottom - (height - top - bottom) * value / ceiling for value in values]
        self.create_line(left - 6, height - bottom, width - right + 10, height - bottom, fill=p["baseline"], width=1)
        if count > 1:
            self.create_line(*[coordinate for point in zip(xs, ys) for coordinate in point], fill=p["series"], width=2,
                             capstyle="round", joinstyle="round")
        marked = range(count) if count <= 12 else (0, count - 1)
        for index in marked:
            self._marker(xs[index], ys[index], p["series"])
        self.create_text(xs[-1] + 9, ys[-1], text=str(values[-1]), anchor="w", fill=p["text"], font=(SEMIBOLD, 10))
        if count > 1 and values[0] != values[-1]:
            self.create_text(xs[0], ys[0] - 9, text=str(values[0]), anchor="s", fill=p["muted"], font=(FONT, 9))
        self.create_text(xs[0], height - bottom + 5, text=entries[0]["at"][:10], anchor="n" if count == 1 else "nw",
                         fill=p["muted"], font=(FONT, 8))
        if count > 1:
            self.create_text(xs[-1], height - bottom + 5, text=entries[-1]["at"][:10], anchor="ne", fill=p["muted"], font=(FONT, 8))
        self.points = list(zip(xs, ys, entries))

    def _marker(self, x: float, y: float, color: str, tag: str = ""):
        self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=palette["surface"], outline="", tags=tag)
        self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="", tags=tag)

    def _hover(self, event):
        self.delete("hover")
        if not self.points:
            return
        x, y, entry = min(self.points, key=lambda point: abs(point[0] - event.x))
        p = palette
        width, height = self.size
        self.create_line(x, 8, x, height - 24, fill=p["grid"], width=1, tags="hover")
        self.tag_lower("hover")
        self._marker(x, y, p["series"], "hover")
        share = 100 * entry["implemented"] / max(1, entry["implemented"] + entry["stub"] + entry["absent"])
        text = f"{entry['at']}  ·  {entry.get('install') or 'AnyPS5'}\n{entry['absent']} missing  ·  {share:.0f}% implemented"
        label = self.create_text(0, 0, text=text, anchor="nw", fill=p["text"], font=(FONT, 9), tags="hover")
        x1, y1, x2, y2 = self.bbox(label)
        box_width, box_height = x2 - x1 + 16, y2 - y1 + 10
        left = min(max(4, x - box_width / 2), width - box_width - 4)
        top_edge = 4 if y > height / 2 else height - box_height - 28
        self.coords(label, left + 8, top_edge + 5)
        box = self.create_rectangle(left, top_edge, left + box_width, top_edge + box_height, fill=p["surface2"],
                                    outline=p["border"], tags="hover")
        self.tag_raise(label, box)
