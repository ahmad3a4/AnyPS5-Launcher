"""Console-style home screen, after the PS5's: the selected game's art fills the window, a row of rounded game tiles
runs along the top with the focused one larger and named, and below it the title, a Play button, an options button and
cards with playtime, compatibility, frame rate and screenshots. Keyboard, mouse and Xbox-style controllers drive it.

Tk cannot draw translucent shapes, so the darkening gradient and the frosted cards are small RGBA PNGs made here and
blended onto the artwork with Tk's own image compositing ("copy -compositingrule overlay").
"""
from __future__ import annotations

import base64
import math
import time
import tkinter as tk
from types import SimpleNamespace
from typing import Optional

import anyps5core as core
from ui_library import shorten
from ui_theme import FONT, SEMIBOLD

LIGHT = "Segoe UI Light"
TEXT, MUTED, DIM, INK = "#ffffff", "#c9d0de", "#8d96a8", "#0b0d12"
GOOD, WARN, BAD = "#6ee39a", "#ffcf6e", "#ff8a80"
BLUE_TOP, BLUE_BOTTOM = (14, 34, 78), (2, 6, 16)
TILE, TILE_FOCUS, TILE_GAP, MARGIN = 88, 124, 14, 56
ROW_TOP = 72
CARD_HEIGHT, CARD_GAP = 150, 16
REPEAT_DELAY, REPEAT_EVERY = 0.35, 0.12   # seconds, for held controller directions


# ---------------------------------------------------------------------------
# Image helpers (pure Tk: no imaging library is installed)


def png_photo(width: int, height: int, data: bytes, alpha: bool) -> tk.PhotoImage:
    return tk.PhotoImage(data=base64.b64encode(core.encode_png(width, height, data, alpha=alpha)))


def blend(target: tk.PhotoImage, source: tk.PhotoImage, x: int = 0, y: int = 0, zoom: tuple[int, int] = (1, 1)) -> None:
    """Draws `source` over `target`, honouring its alpha."""
    target.tk.call(target, "copy", source, "-to", x, y, "-zoom", *zoom, "-compositingrule", "overlay")


def vertical_strip(height: int, stops: list[tuple[float, tuple[int, ...]]]) -> tk.PhotoImage:
    """A 1-pixel-wide gradient (RGB or RGBA by the stops' length), zoomed out to full width when used."""
    alpha = len(stops[0][1]) == 4
    rows = []
    for row in range(height):
        t = row / max(1, height - 1)
        for (start, first), (end, second) in zip(stops, stops[1:]):
            if start <= t <= end:
                k = (t - start) / max(1e-9, end - start)
                rows.append(bytes(round(a + (b - a) * k) for a, b in zip(first, second)))
                break
        else:
            rows.append(bytes(stops[-1][1]))
    return png_photo(1, height, b"".join(rows), alpha)


def rounded_panel(width: int, height: int, radius: int, rgba: tuple[int, int, int, int]) -> tk.PhotoImage:
    """A translucent rounded rectangle with anti-aliased corners."""
    red, green, blue, alpha = rgba
    full = bytes((red, green, blue, alpha)) * width
    rows = []
    for row in range(height):
        if row < radius:
            dy = radius - row - 0.5
        elif row >= height - radius:
            dy = row + 0.5 - (height - radius)
        else:
            rows.append(full)
            continue
        line = bytearray(full)
        for column in range(radius):
            coverage = max(0.0, min(1.0, radius - math.hypot(radius - column - 0.5, dy) + 0.5))
            line[column * 4 + 3] = line[(width - 1 - column) * 4 + 3] = int(alpha * coverage)
        rows.append(bytes(line))
    return png_photo(width, height, b"".join(rows), True)


def ratio(scale: float, at_least: bool) -> tuple[int, int]:
    """Small zoom/subsample pair approximating `scale` (never below it when `at_least`)."""
    best = (1, 1)
    best_error = float("inf")
    for zoom in range(1, 9):
        for sub in range(1, 13):
            value = zoom / sub
            if at_least and value < scale:
                continue
            error = abs(value - scale)
            if error < best_error:
                best, best_error = (zoom, sub), error
    return best


def scaled(image: tk.PhotoImage, scale: float, at_least: bool = False) -> tk.PhotoImage:
    zoom, sub = ratio(scale, at_least)
    result = tk.PhotoImage()
    result.tk.call(result, "copy", image, "-subsample", sub, sub, "-zoom", zoom, zoom)
    return result


def crop(image: tk.PhotoImage, width: int, height: int) -> tk.PhotoImage:
    left, top = max(0, (image.width() - width) // 2), max(0, (image.height() - height) // 2)
    result = tk.PhotoImage(width=width, height=height)
    result.tk.call(result, "copy", image, "-from", left, top, left + min(width, image.width()), top + min(height, image.height()),
                   "-to", 0, 0)
    return result


def rounded_icon(image: tk.PhotoImage, size: int, radius: int) -> tk.PhotoImage:
    icon = crop(scaled(image, size / max(1, image.width()), at_least=True), size, size)
    for row in range(radius):
        for column in range(radius):
            if math.hypot(radius - column - 0.5, radius - row - 0.5) > radius:
                for x, y in ((column, row), (size - 1 - column, row), (column, size - 1 - row), (size - 1 - column, size - 1 - row)):
                    icon.transparency_set(x, y, True)
    return icon


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **options):
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius, x2, y2 - radius, x2, y2,
              x2 - radius, y2, x1 + radius, y2, x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


# ---------------------------------------------------------------------------


class ConsoleView(tk.Frame):
    def __init__(self, master: tk.Misc, app):
        super().__init__(master, bg="#02050c", highlightthickness=0, bd=0)
        self.app = app
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg="#02050c", takefocus=1)
        self.canvas.pack(fill="both", expand=True)
        self.games: list[core.Game] = []
        self.index = 0
        self.first = 0
        self.visible = False
        self.fps: Optional[float] = None
        self.art: dict[str, Optional[tk.PhotoImage]] = {}
        self.icons: dict[tuple[str, int], tk.PhotoImage] = {}
        self.panels: dict[tuple[int, int], tk.PhotoImage] = {}
        self.thumbs: dict[str, tk.PhotoImage] = {}
        self.background: Optional[tk.PhotoImage] = None
        self.background_key = None
        self.pending = None
        self.pad = core.XInput()
        self.pad_last: set[str] = set()
        self.pad_direction = 0
        self.pad_next = 0.0
        canvas = self.canvas
        canvas.bind("<Configure>", lambda _event: self.schedule())
        for key, action in (("<Left>", lambda: self.move(-1)), ("<Right>", lambda: self.move(1)),
                            ("<Home>", lambda: self.move(-len(self.games))), ("<End>", lambda: self.move(len(self.games))),
                            ("<Return>", self.activate), ("<space>", self.activate), ("<F11>", self.toggle_fullscreen),
                            ("<Escape>", self.leave_fullscreen), ("<Menu>", self.options), ("<Delete>", self.app.remove_selected)):
            canvas.bind(key, lambda _event, action=action: action())
        canvas.bind("<MouseWheel>", lambda event: self.move(-1 if event.delta > 0 else 1))
        canvas.bind("<Button-1>", lambda _event: canvas.focus_set(), add="+")

    # ----- showing ----------------------------------------------------------

    def show(self):
        self.visible = True
        self.pack(fill="both", expand=True)
        self.refresh()
        self.canvas.focus_set()
        self.after(1000, self.tick_clock)
        self.after(60, self.poll_pad)

    def hide(self):
        self.visible = False
        self.leave_fullscreen()
        self.pack_forget()

    def refresh(self):
        """Reloads the games (most recently played first, as a console does) and redraws."""
        if not self.visible:
            return
        keep = self.app.selected_id
        self.games = sorted(self.app.library.games, key=lambda game: (game.last_played or "", game.added or ""), reverse=True)
        ids = [game.id for game in self.games]
        self.index = ids.index(keep) if keep in ids else 0
        if self.games:
            self.app.selected_id = self.games[self.index].id
        self.layout()

    def schedule(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(60, self.layout)

    def selected(self) -> Optional[core.Game]:
        return self.games[self.index] if self.games else None

    # ----- input ------------------------------------------------------------

    def move(self, delta: int):
        if not self.games:
            return
        index = max(0, min(len(self.games) - 1, self.index + delta))
        if index != self.index:
            self.index = index
            self.app.selected_id = self.games[index].id
            self.fps = None
            self.layout()

    def select(self, index: int):
        self.move(index - self.index)
        self.canvas.focus_set()

    def activate(self):
        game = self.selected()
        if game is not None:
            self.app.selected_id = game.id
            self.app.play(game)
            self.after(300, self.layout)

    def options(self, x: Optional[int] = None, y: Optional[int] = None):
        game = self.selected()
        if game is None:
            return
        if x is None:
            x, y = self.canvas.winfo_rootx() + MARGIN, self.canvas.winfo_rooty() + self.canvas.winfo_height() // 2
        self.app.context_menu(game.id, SimpleNamespace(x_root=x, y_root=y))
        self.after(300, self.layout)

    def toggle_fullscreen(self):
        root = self.winfo_toplevel()
        root.attributes("-fullscreen", not root.attributes("-fullscreen"))

    def leave_fullscreen(self):
        root = self.winfo_toplevel()
        if root.attributes("-fullscreen"):
            root.attributes("-fullscreen", False)

    def poll_pad(self):
        """Xbox-style controller: D-pad or left stick to choose, A to play, Y for options, View for the classic view."""
        if not self.visible:
            return
        state = None
        if self.pad.available and core.foreground_window() == self.app_window():
            for index in range(4):
                state = self.pad.read(index)
                if state:
                    break
        buttons = state["buttons"] if state else set()
        direction = 0
        if "Left" in buttons or (state and state["lx"] < -0.55):
            direction = -1
        elif "Right" in buttons or (state and state["lx"] > 0.55):
            direction = 1
        now = time.monotonic()
        if direction and (direction != self.pad_direction or now >= self.pad_next):
            self.move(direction)
            self.pad_next = now + (REPEAT_DELAY if direction != self.pad_direction else REPEAT_EVERY)
        self.pad_direction = direction
        pressed = buttons - self.pad_last
        self.pad_last = buttons
        if "Cross" in pressed or "Options" in pressed:
            self.activate()
        elif "Triangle" in pressed:
            self.options()
        elif "TouchPad" in pressed:
            self.app.show_classic()
            return
        self.after(50, self.poll_pad)

    def app_window(self) -> int:
        try:
            import ctypes
            return ctypes.windll.user32.GetParent(self.winfo_toplevel().winfo_id())
        except (AttributeError, OSError):
            return 0

    def tick_clock(self):
        if self.visible:
            self.canvas.itemconfigure("clock", text=time.strftime("%H:%M"))
            self.after(15000, self.tick_clock)

    def set_fps(self, fps: float):
        self.fps = fps
        if self.visible:
            self.canvas.itemconfigure("status", text=self.status()[0])

    # ----- drawing ----------------------------------------------------------

    def geometry(self, width: int, height: int) -> dict:
        hero_top = max(ROW_TOP + TILE_FOCUS + 60, int(height * 0.36))
        cards_top = height - CARD_HEIGHT - 52
        card_width = max(180, min(300, (width - 2 * MARGIN - 3 * CARD_GAP) // 4))
        cards = [(MARGIN + index * (card_width + CARD_GAP), cards_top) for index in range(4)]
        return {"hero": hero_top, "cards": cards, "card_width": card_width}

    def artwork(self, game: Optional[core.Game]) -> Optional[tk.PhotoImage]:
        if game is None:
            return None
        if game.id not in self.art:
            picture = game.source / "sce_sys" / "pic0.png"
            try:
                self.art[game.id] = tk.PhotoImage(file=str(picture)) if picture.is_file() else None
            except tk.TclError:
                self.art[game.id] = None
        return self.art[game.id]

    def compose_background(self, game: Optional[core.Game], width: int, height: int, layout: dict) -> tk.PhotoImage:
        key = (game.id if game else None, width, height)
        if key == self.background_key and self.background is not None:
            return self.background
        art = self.artwork(game)
        if art is not None:
            image = crop(scaled(art, max(width / art.width(), height / art.height()), at_least=True), width, height)
            shade = vertical_strip(height, [(0.0, (0, 0, 0, 150)), (0.35, (0, 0, 0, 90)), (0.62, (2, 6, 16, 200)),
                                            (1.0, (2, 6, 16, 245))])
        else:
            image = tk.PhotoImage(width=width, height=height)
            blend(image, vertical_strip(height, [(0.0, BLUE_TOP), (1.0, BLUE_BOTTOM)]), zoom=(width, 1))
            shade = None
        if shade is not None:
            blend(image, shade, zoom=(width, 1))
        size = (layout["card_width"], CARD_HEIGHT)
        if size not in self.panels:
            self.panels[size] = rounded_panel(*size, 14, (20, 26, 40, 175))
        for x, y in layout["cards"]:
            blend(image, self.panels[size], x, y)
        self.background, self.background_key = image, key
        return image

    def tile(self, game: core.Game, size: int) -> tk.PhotoImage:
        key = (game.id, size)
        if key not in self.icons:
            try:
                source = tk.PhotoImage(file=str(game.icon)) if game.icon.is_file() else None
            except tk.TclError:
                source = None
            if source is None:
                source = tk.PhotoImage(width=64, height=64)
                source.put("#24324f", to=(0, 0, 64, 64))
                source.put("#3b4d73", to=(20, 20, 44, 44))
            self.icons[key] = rounded_icon(source, size, max(10, size // 9))
        return self.icons[key]

    def status(self) -> tuple[str, str]:
        game = self.selected()
        if game is None:
            return "", MUTED
        if self.app.is_running(game):
            return (f"Running  ·  {self.fps:.0f} FPS" if self.fps else "Running"), GOOD
        category = self.app.category(game)
        text = {"ok": "Ready to play", "warn": "AnyPS5 changed since this game was converted: it converts again first",
                "idle": "Not converted yet: Play converts it first", "blocked": "Won't start yet: AnyPS5 lacks functions it needs"}[category]
        return text, {"ok": GOOD, "warn": WARN, "idle": MUTED, "blocked": BAD}[category]

    def layout(self):
        self.pending = None
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.visible or width < 100 or height < 100:
            return
        canvas.delete("all")
        geometry = self.geometry(width, height)
        game = self.selected()
        canvas.create_image(0, 0, anchor="nw", image=self.compose_background(game, width, height, geometry))
        self.draw_top_bar(width)
        if game is None:
            canvas.create_text(MARGIN, geometry["hero"], anchor="nw", text="Your games will appear here", fill=TEXT,
                               font=(LIGHT, 30))
            canvas.create_text(MARGIN, geometry["hero"] + 64, anchor="nw", fill=MUTED, font=(FONT, 12),
                               text="Add a game folder with + at the top right, or switch to the classic view.")
            return
        self.draw_row(width)
        self.draw_hero(game, geometry["hero"], width)
        self.draw_cards(game, geometry)
        canvas.create_text(MARGIN, height - 22, anchor="w", fill=DIM, font=(FONT, 9),
                           text="◀ ▶  choose      Enter or A  play      Y or right-click  options      F11  full screen      "
                                "View  classic view")

    def draw_top_bar(self, width: int):
        canvas = self.canvas
        canvas.create_text(MARGIN, 34, anchor="w", text="Games", fill=TEXT, font=(SEMIBOLD, 15))
        library = canvas.create_text(MARGIN + 82, 34, anchor="w", text="Library", fill=DIM, font=(FONT, 15), tags="link")
        canvas.tag_bind(library, "<Button-1>", lambda _event: self.app.show_classic())
        x = width - MARGIN
        clock = canvas.create_text(x, 34, anchor="e", text=time.strftime("%H:%M"), fill=TEXT, font=(LIGHT, 16), tags="clock")
        x = canvas.bbox(clock)[0] - 28
        for glyph, action, hint in (("⚙", self.app.open_settings, "settings"), ("☰", self.app.show_classic, "classic"),
                                    ("＋", self.app.add_game, "add")):
            item = canvas.create_text(x, 34, anchor="e", text=glyph, fill=MUTED, font=(FONT, 16), tags=("icon", hint))
            canvas.tag_bind(item, "<Button-1>", lambda _event, action=action: action())
            canvas.tag_bind(item, "<Enter>", lambda _event, item=item: canvas.itemconfigure(item, fill=TEXT))
            canvas.tag_bind(item, "<Leave>", lambda _event, item=item: canvas.itemconfigure(item, fill=MUTED))
            x = canvas.bbox(item)[0] - 22

    def draw_row(self, width: int):
        canvas = self.canvas
        room = max(1, (width - 2 * MARGIN - TILE_FOCUS - 220) // (TILE + TILE_GAP) + 1)
        if self.index < self.first:
            self.first = self.index
        elif self.index > self.first + room - 1:
            self.first = self.index - room + 1
        x = MARGIN
        for position in range(self.first, len(self.games)):
            game = self.games[position]
            focused = position == self.index
            size = TILE_FOCUS if focused else TILE
            y = ROW_TOP + (TILE_FOCUS - size) // 2
            tag = f"tile{position}"
            canvas.create_image(x, y, anchor="nw", image=self.tile(game, size), tags=tag)
            if focused:
                round_rect(canvas, x - 5, y - 5, x + size + 5, y + size + 5, 18, outline=TEXT, width=3, fill="")
                name = canvas.create_text(x + size + 18, y + size // 2, anchor="w", fill=TEXT, font=(FONT, 12),
                                          text=shorten(("★ " if game.favorite else "") + game.title, 30))
                x = canvas.bbox(name)[2] + 26
            else:
                x += size + TILE_GAP
            canvas.tag_bind(tag, "<Button-1>", lambda _event, position=position: self.select(position))
            canvas.tag_bind(tag, "<Double-Button-1>", lambda _event: self.activate())
            canvas.tag_bind(tag, "<Button-3>", lambda event, position=position: (self.select(position),
                                                                                self.options(event.x_root, event.y_root)))
            if x > width - MARGIN:
                break

    def draw_hero(self, game: core.Game, top: int, width: int):
        canvas = self.canvas
        title = canvas.create_text(MARGIN, top, anchor="nw", text=game.title, fill=TEXT, font=(LIGHT, 34),
                                   width=int(width * 0.62))
        y = canvas.bbox(title)[3] + 8
        text, color = self.status()
        canvas.create_text(MARGIN, y, anchor="nw", text=text, fill=color, font=(FONT, 11), tags="status")
        y += 44
        running = self.app.is_running(game)
        label = "Stop" if running else ("Play" if self.app.category(game) in ("ok", "blocked") else "Convert and play")
        button_width = 150 if label in ("Play", "Stop") else 200
        play = round_rect(canvas, MARGIN, y, MARGIN + button_width, y + 46, 23, fill=TEXT, outline="", tags="play")
        canvas.create_text(MARGIN + button_width // 2, y + 23, text=label, fill=INK, font=(SEMIBOLD, 12), tags="play")
        canvas.tag_bind("play", "<Button-1>", lambda _event: self.activate())
        canvas.tag_bind("play", "<Enter>", lambda _event: canvas.itemconfigure(play, fill="#e6e8ee"))
        canvas.tag_bind("play", "<Leave>", lambda _event: canvas.itemconfigure(play, fill=TEXT))
        x = MARGIN + button_width + 16
        ring = canvas.create_oval(x, y, x + 46, y + 46, outline=MUTED, width=2, fill="", tags="more")
        canvas.create_text(x + 23, y + 21, text="•••", fill=TEXT, font=(SEMIBOLD, 11), tags="more")
        canvas.tag_bind("more", "<Button-1>", lambda event: self.options(event.x_root, event.y_root))
        canvas.tag_bind("more", "<Enter>", lambda _event: canvas.itemconfigure(ring, outline=TEXT))
        canvas.tag_bind("more", "<Leave>", lambda _event: canvas.itemconfigure(ring, outline=MUTED))

    def draw_cards(self, game: core.Game, geometry: dict):
        canvas, card_width = self.canvas, geometry["card_width"]
        played = (core.format_duration(game.playtime), f"{game.sessions} session{'s' if game.sessions != 1 else ''}"
                  + (f"  ·  last {game.last_played[:10]}" if game.last_played else "")) if game.sessions else \
                 ("Not played yet", "Playtime is counted per session")
        if game.audit:
            share = core.implemented_share(game.audit)
            verdict, level = core.audit_verdict(game.audit)
            short = {"ok": "Every function it needs is there", "warn": f"{game.audit['stub']} unfinished functions",
                     "error": f"{game.audit['absent']} functions missing"}[level]
            compatibility = (f"{share:.0f}%", short)
        else:
            compatibility = ("—", "Checked after converting")
        last = game.performance[-1] if game.performance else None
        frame_rate = (f"{last['avg']:.0f} FPS", f"lowest {last['low']:.0f}  ·  last session") if last else \
                     ("—", "Measured while you play")
        shots = core.list_screenshots(game)
        cards = [("Your progress", *played, lambda: self.app.show_classic()),
                 ("Compatibility", *compatibility, self.app.open_history),
                 ("Frame rate", *frame_rate, lambda: self.app.show_classic()),
                 ("Screenshots", f"{len(shots)}" if shots else "None yet",
                  "Click to open the folder" if shots else f"Press {self.app.settings.screenshot_hotkey} while playing",
                  self.app.open_screenshots)]
        for (x, y), (heading, value, detail, action) in zip(geometry["cards"], cards):
            tag = f"card-{heading}"
            canvas.create_rectangle(x, y, x + card_width, y + CARD_HEIGHT, outline="", fill="", tags=tag)
            canvas.create_text(x + 18, y + 16, anchor="nw", text=heading.upper(), fill=DIM, font=(SEMIBOLD, 8), tags=tag)
            if heading == "Screenshots" and shots:
                thumb = self.thumbnail(shots[0], card_width - 36)
                if thumb is not None:
                    canvas.create_image(x + 18, y + 38, anchor="nw", image=thumb, tags=tag)
                    canvas.create_text(x + card_width - 18, y + 16, anchor="ne", text=value, fill=MUTED,
                                       font=(SEMIBOLD, 9), tags=tag)
                    canvas.tag_bind(tag, "<Button-1>", lambda _event, action=action: action())
                    continue
            canvas.create_text(x + 18, y + 40, anchor="nw", text=value, fill=TEXT, font=(LIGHT, 24), tags=tag)
            canvas.create_text(x + 18, y + 90, anchor="nw", text=detail, fill=MUTED, font=(FONT, 9),
                               width=card_width - 36, tags=tag)
            canvas.tag_bind(tag, "<Button-1>", lambda _event, action=action: action())

    def thumbnail(self, shot, width: int) -> Optional[tk.PhotoImage]:
        thumb_path = core.screenshot_thumbnail(shot)
        source = thumb_path if thumb_path.is_file() else shot
        key = f"{source}:{width}"
        if key not in self.thumbs:
            try:
                image = tk.PhotoImage(file=str(source))
            except tk.TclError:
                return None
            fitted = scaled(image, min(width / image.width(), (CARD_HEIGHT - 54) / image.height()))
            self.thumbs[key] = fitted
        return self.thumbs[key]
