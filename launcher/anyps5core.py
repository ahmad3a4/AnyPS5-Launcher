"""Engine of the AnyPS5 launcher: game library, SELF unwrapping, conversion, launching and updates.

Nothing here imports tkinter, so every step can be tested or scripted without a window.
Long-running functions take a `log(text, tag)` callback; tags are info, cmd, out, ok, warn, error, hint.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

Log = Callable[[str, str], None]

LAUNCHER_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = LAUNCHER_DIR.parent
DATA_DIR = Path(os.environ.get("ANYPS5_LAUNCHER_DATA") or LAUNCHER_DIR / "data")
LIBRARY_FILE = DATA_DIR / "library.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
INPUT_FILE = DATA_DIR / "anyps5-input.ini"
FONTS_DIR = DATA_DIR / "fonts"
BACKUPS_DIR = DATA_DIR / "backups"
INSTANCE_FILE = DATA_DIR / "instance.json"
LAUNCHER_SCRIPT = LAUNCHER_DIR / "AnyPS5 Launcher.pyw"
SAVES_DIR_NAME = "_sd"          # libSceSaveData.native keeps saves here, relative to the game's working directory
AUTO_BACKUPS_KEPT = 10
HISTORY_KEPT = 100

LATEST_INSTALL_NAME = "AnyPS5-latest"
DEFAULT_SOURCE_DIR = r"C:\dev\AnyPS5"
DEFAULT_TOOLCHAIN_DIR = r"C:\winlibs\mingw64\bin"
GIT_CA_BUNDLE = Path(r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt")
RELEASES_API = "https://api.github.com/repos/boykopovar/AnyPS5/releases/latest"

MODULE_DIR_NAMES = ("sce_module", "sce_modules", "prx")
EXECUTABLE_CANDIDATES = ("eboot.bin", "eboot.elf", "eboot.self")
OUTPUT_EXE = "eboot.exe"
STAGING_DIR = "_input"
MARKER_FILE = ".anyps5-launcher.json"
RUN_LOG = "anyps5-last-run.log"
UPDATE_LOG = "anyps5-last-update.log"
AUDIT_JSON = "anyps5-audit.json"
RUNTIME_DLLS = ("libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")

ELF_MAGIC = b"\x7fELF"
SELF_MAGICS = (b"\x4f\x15\x3d\x1d", b"\x54\x14\xf5\xee")
PT_SCE_VERSION = 0x6FFFFF01
SELF_ENTRY_ENCRYPTED = 0x2
SELF_ENTRY_COMPRESSED = 0x8
SELF_ENTRY_BLOCKED = 0x800

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Console language numbers from docs/user/USAGE.md; 1 is the runtime default.
LANGUAGES = [
    "Japanese", "English (United States)", "French", "Spanish", "German", "Italian", "Dutch",
    "Portuguese (Portugal)", "Russian", "Korean", "Chinese (Traditional)", "Chinese (Simplified)",
    "Finnish", "Swedish", "Danish", "Norwegian", "Polish", "Portuguese (Brazil)", "English (United Kingdom)",
    "Turkish", "Spanish (Latin America)", "Arabic", "French (Canada)", "Czech", "Hungarian", "Greek",
    "Romanian", "Thai", "Vietnamese", "Indonesian", "Ukrainian",
]
DEFAULT_LANGUAGE = 1
THREAD_PRIORITIES = ("off", "high", "rt")

# Built-in keyboard and mouse bindings, from core/libs/prx/libScePad/include/InputMapping.hpp.
INPUT_ACTIONS = {
    "Cross": "KEY:Return, KEY:Space", "Circle": "KEY:C", "Triangle": "KEY:I", "Square": "MOUSE:Left",
    "L1": "KEY:Q", "R1": "KEY:E, KEY:Left Alt, KEY:Right Alt", "L2": "", "R2": "MOUSE:Right",
    "L3": "KEY:Left Shift, KEY:Right Shift", "R3": "KEY:Left Ctrl, KEY:Right Ctrl", "Options": "KEY:Escape",
    "Up": "KEY:Up, WHEEL:Up", "Right": "KEY:Right", "Down": "KEY:Down, WHEEL:Down", "Left": "KEY:Left",
    "LeftStickLeft": "KEY:A", "LeftStickRight": "KEY:D", "LeftStickUp": "KEY:W", "LeftStickDown": "KEY:S",
    "RightStickLeft": "KEY:F", "RightStickRight": "KEY:H", "RightStickUp": "KEY:T", "RightStickDown": "KEY:G",
    "TouchLeft": "KEY:Backspace", "TouchRight": "KEY:Tab", "ToggleMouse": "MOUSE:Middle", "ToggleFullscreen": "KEY:F11",
}
MOUSE_BUTTONS = ("Left", "Middle", "Right", "X1", "X2")


class LauncherError(Exception):
    """A failure with a message meant for the person using the launcher."""


# ---------------------------------------------------------------------------
# Files and paths


def _long(path: Path) -> str:
    """Extended-length form so deep game trees work while Windows long paths are disabled."""
    text = str(Path(path).absolute())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
    return text


def file_magic(path: Path) -> bytes:
    try:
        with open(_long(path), "rb") as stream:
            return stream.read(4)
    except OSError:
        return b""


def file_kind(path: Path) -> str:
    magic = file_magic(path)
    if magic == ELF_MAGIC:
        return "elf"
    if magic in SELF_MAGICS:
        return "self"
    return "other"


def link_or_copy(source: Path, target: Path) -> bool:
    """Hard-links when possible (no extra space, originals untouched); returns False when it had to copy."""
    try:
        os.link(_long(source), _long(target))
        return True
    except OSError:
        shutil.copy2(_long(source), _long(target))
        return False


def can_hardlink(source_dir: Path, target_dir: Path) -> bool:
    """True when files of source_dir can be hard-linked into target_dir (same NTFS volume)."""
    sample = next((path for path in Path(source_dir).rglob("*") if path.is_file()), None)
    probe_dir = Path(target_dir)
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    if sample is None:
        return True
    probe = probe_dir / f".anyps5-link-probe-{uuid.uuid4().hex[:8]}"
    try:
        os.link(sample, probe)
    except OSError:
        return False
    os.unlink(probe)
    return True


def folder_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(_long(path)):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def is_inside(child: Path, parent: Path) -> bool:
    child, parent = Path(os.path.abspath(child)), Path(os.path.abspath(parent))
    return child == parent or parent in child.parents


def remove_tree(path: Path, owner: Path) -> None:
    """Deletes a launcher-made directory. Refuses anything outside `owner`; links are removed, never followed."""
    if not path.exists() and not path.is_symlink():
        return
    if not is_inside(path, owner) or Path(os.path.abspath(path)) == Path(os.path.abspath(owner)):
        raise LauncherError(f"Refusing to delete {path}: it is not inside {owner}")
    if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        os.unlink(path) if path.is_symlink() and not path.is_dir() else os.rmdir(path)
        return

    def clear_read_only(function, failed_path, _error):
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    shutil.rmtree(_long(path), onexc=clear_read_only)


def _raise(error: OSError) -> None:
    raise error


def install_fingerprint(directory: Path) -> str:
    """Changes whenever the relinker or any system library changes (an update may rebuild only the libraries)."""
    digest = hashlib.sha256()
    with open(directory / "relinker.exe", "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    for library in sorted((directory / "libs").iterdir()):
        info = library.stat()
        digest.update(f"{library.name}|{info.st_size}|{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:12]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary name, so a background job and the window can save at the same moment.
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def open_in_explorer(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - opens a folder the user picked
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# SELF containers


@dataclass
class SelfEntry:
    props: int
    offset: int
    filesz: int
    memsz: int

    @property
    def segment(self) -> int:
        return (self.props >> 20) & 0xFFFF


def read_self_entries(data: bytes) -> list[SelfEntry]:
    if data[:4] not in SELF_MAGICS:
        raise LauncherError("Not a SELF container")
    if len(data) < 0x20:
        raise LauncherError("SELF header is truncated")
    count = struct.unpack_from("<H", data, 0x18)[0]
    if 0x20 + count * 0x20 > len(data):
        raise LauncherError("SELF entry table is truncated")
    return [SelfEntry(*struct.unpack_from("<QQQQ", data, 0x20 + index * 0x20)) for index in range(count)]


def self_is_encrypted(data: bytes) -> bool:
    return any(entry.props & SELF_ENTRY_ENCRYPTED for entry in read_self_entries(data))


def unwrap_fake_self(data: bytes) -> bytes:
    """Rebuilds the plain ELF carried by a fake-signed (unencrypted) SELF.

    This only rearranges bytes that are already in the clear; an encrypted SELF is refused,
    because it needs a decrypted dump that the launcher does not and will not produce.
    """
    entries = read_self_entries(data)
    if any(entry.props & SELF_ENTRY_ENCRYPTED for entry in entries):
        raise LauncherError("The executable is an encrypted (retail) SELF. AnyPS5 needs a decrypted dump of the game; "
                            "the launcher only unwraps fake-signed homebrew and cannot decrypt anything.")
    elf = 0x20 + len(entries) * 0x20
    if data[elf:elf + 4] != ELF_MAGIC:
        raise LauncherError("The SELF container does not carry an ELF header")
    phoff = struct.unpack_from("<Q", data, elf + 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, elf + 0x36)
    if phentsize != 0x38 or elf + phoff + phnum * phentsize > len(data):
        raise LauncherError("The SELF container has a malformed program header table")
    headers = [struct.unpack_from("<IIQQQQQQ", data, elf + phoff + index * phentsize) for index in range(phnum)]
    table_end = phoff + phnum * phentsize
    size = max([table_end] + [header[2] + header[5] for header in headers])
    output = bytearray(size)
    output[0:0x40] = data[elf:elf + 0x40]
    output[phoff:table_end] = data[elf + phoff:elf + table_end]
    struct.pack_into("<Q", output, 0x28, 0)          # e_shoff: section headers are not carried in a SELF
    struct.pack_into("<HHH", output, 0x3A, 0, 0, 0)  # e_shentsize, e_shnum, e_shstrndx
    covered = set()
    for entry in entries:
        if not entry.props & SELF_ENTRY_BLOCKED:
            continue
        if entry.segment >= phnum:
            raise LauncherError(f"SELF segment {entry.segment} has no program header")
        chunk = data[entry.offset:entry.offset + entry.filesz]
        if len(chunk) != entry.filesz:
            raise LauncherError(f"SELF segment {entry.segment} is truncated")
        if entry.props & SELF_ENTRY_COMPRESSED:
            chunk = zlib.decompress(chunk)
        _, _, p_offset, _, _, p_filesz, _, _ = headers[entry.segment]
        if len(chunk) != p_filesz:
            raise LauncherError(f"SELF segment {entry.segment} holds {len(chunk)} bytes, its program header expects {p_filesz}")
        output[p_offset:p_offset + p_filesz] = chunk
        covered.add(entry.segment)
    entries_end = max((entry.offset + entry.filesz for entry in entries), default=0)
    for index, (p_type, _, p_offset, _, _, p_filesz, _, _) in enumerate(headers):
        # The version segment is not listed in the entry table; it trails the file.
        if p_type == PT_SCE_VERSION and index not in covered and 0 < p_filesz <= len(data) - entries_end:
            output[p_offset:p_offset + p_filesz] = data[len(data) - p_filesz:]
    return bytes(output)


# ---------------------------------------------------------------------------
# AnyPS5 installations


@dataclass
class Install:
    name: str
    path: Path
    label: str
    sha: str

    @property
    def relinker(self) -> Path:
        return self.path / "relinker.exe"

    @property
    def libs(self) -> Path:
        return self.path / "libs"


def find_installs(root: Path = WORKSPACE_DIR) -> list[Install]:
    installs = []
    for directory in sorted(root.iterdir() if root.is_dir() else []):
        relinker = directory / "relinker.exe"
        if directory.suffix in (".new", ".old") or not relinker.is_file() or not any((directory / "libs").glob("*.prx")):
            continue
        version = directory / "VERSION.txt"
        label = version.read_text(encoding="utf-8-sig").strip() if version.is_file() else directory.name
        installs.append(Install(directory.name, directory, label, install_fingerprint(directory)))
    installs.sort(key=lambda install: (install.name != LATEST_INSTALL_NAME, install.name))
    return installs


# ---------------------------------------------------------------------------
# Games


@dataclass
class GameSettings:
    gpu: str = ""                    # part of a Vulkan device name; empty picks automatically
    language: int = DEFAULT_LANGUAGE
    to_intel: bool = False
    diagnostics: bool = False
    unused_filter: int = 0
    thread_priority: str = "off"
    store_at_flip: bool = False
    module_dirs: str = ""            # extra --module-dir folders, separated by ';'
    env: str = ""                    # extra KEY=VALUE pairs, separated by ';'
    fullscreen: bool = False         # press the runtime's F11 toggle once the game shows frames

    def relink_options(self) -> dict:
        return {"to_intel": self.to_intel, "diagnostics": self.diagnostics,
                "unused_filter": self.unused_filter, "module_dirs": self.module_dir_list()}

    def module_dir_list(self) -> list[str]:
        return [part.strip().replace("\\", "/").strip("/") for part in self.module_dirs.split(";") if part.strip()]


@dataclass
class Game:
    id: str
    title: str
    title_id: str
    version: str
    source_dir: str
    executable: str
    output_dir: str
    settings: GameSettings = field(default_factory=GameSettings)
    converted: Optional[dict] = None
    audit: Optional[dict] = None
    last_played: Optional[str] = None
    playtime: int = 0                # seconds, all sessions
    sessions: int = 0
    last_session: int = 0            # seconds
    audit_history: list = field(default_factory=list)
    added: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    performance: list = field(default_factory=list)   # per measured session: at, avg, low, high, seconds
    favorite: bool = False
    tags: list = field(default_factory=list)
    notes: str = ""
    restore_on_convert: bool = False  # set by an import: put the newest save backup back after the first conversion

    @property
    def source(self) -> Path:
        return Path(self.source_dir)

    @property
    def output(self) -> Path:
        return Path(self.output_dir)

    @property
    def icon(self) -> Path:
        return self.source / "sce_sys" / "icon0.png"

    @staticmethod
    def from_dict(value: dict) -> "Game":
        known = {name for name in GameSettings.__dataclass_fields__}
        settings = GameSettings(**{key: item for key, item in value.get("settings", {}).items() if key in known})
        fields = {key: item for key, item in value.items() if key in Game.__dataclass_fields__ and key != "settings"}
        return Game(settings=settings, **fields)


@dataclass
class GameInfo:
    folder: Path
    executable: Path
    kind: str
    title: str
    title_id: str
    version: str
    module_dirs: list[str]
    suggested_module_dirs: list[str]


def read_param_json(folder: Path) -> tuple[str, str, str]:
    """(title, title id, content version) from sce_sys/param.json, with the parser's language fallback."""
    value = read_json(folder / "sce_sys" / "param.json", None)
    if not isinstance(value, dict):
        return folder.name, "", ""
    localized = value.get("localizedParameters") or {}
    language = localized.get("defaultLanguage") or value.get("defaultLanguage") or ""
    entry = localized.get(language) if isinstance(localized.get(language), dict) else None
    if entry is None:
        entry = next((item for item in localized.values() if isinstance(item, dict)), {})
    return entry.get("titleName") or folder.name, value.get("titleId", ""), value.get("contentVersion", "")


def suggest_module_dirs(folder: Path, limit: int = 20000) -> list[str]:
    """Folders besides sce_module that hold ELF modules, such as Unity's Media/Plugins."""
    found, seen = set(), 0
    for root, dirs, files in os.walk(folder):
        relative = Path(root).relative_to(folder)
        if relative.parts and relative.parts[0] in MODULE_DIR_NAMES + ("sce_sys",):
            dirs[:] = []
            continue
        if len(relative.parts) >= 5:
            dirs[:] = []
        for name in files:
            seen += 1
            if name.lower().endswith((".prx", ".sprx")) and relative.parts and file_kind(Path(root) / name) != "other":
                found.add(relative.as_posix())
        if seen > limit:
            break
    return sorted(found)


def inspect_game_folder(folder: Path) -> GameInfo:
    folder = Path(folder)
    if not folder.is_dir():
        raise LauncherError(f"{folder} is not a folder")
    executable = next((folder / name for name in EXECUTABLE_CANDIDATES if (folder / name).is_file()), None)
    if executable is None:
        elves = sorted(path for path in folder.glob("*.elf") if path.is_file())
        executable = elves[0] if elves else None
    if executable is None:
        raise LauncherError("No game executable found. Pick the game's root folder: the one that contains eboot.bin and sce_sys/.")
    kind = file_kind(executable)
    if kind == "other":
        raise LauncherError(f"{executable.name} is neither an ELF nor a SELF file.")
    if kind == "self" and self_is_encrypted(executable.read_bytes()):
        raise LauncherError(f"{executable.name} is encrypted (a retail SELF). AnyPS5 needs a decrypted dump of the game; "
                            "the launcher only unwraps fake-signed homebrew and cannot decrypt anything.")
    title, title_id, version = read_param_json(folder)
    module_dirs = [name for name in MODULE_DIR_NAMES if (folder / name).is_dir()]
    return GameInfo(folder, executable, kind, title, title_id, version, module_dirs, suggest_module_dirs(folder))


def default_output_dir(folder: Path) -> Path:
    return folder.parent / f"{folder.name}-anyps5"


def module_files(game: Game) -> list[Path]:
    """Module files the relinker converts; they are staged and left out of the app0 mirror."""
    files = []
    directories = [game.source / name for name in MODULE_DIR_NAMES]
    directories += [game.source / relative for relative in game.settings.module_dir_list()]
    for directory in directories:
        if directory.is_dir():
            files += [path for path in sorted(directory.iterdir()) if path.is_file() and file_kind(path) != "other"]
    return files


class Library:
    # Defaults are looked up at call time (not bound when the module loads), so patched paths are honoured.
    def __init__(self, path: Optional[Path] = None):
        self.path = path or LIBRARY_FILE
        self.games: list[Game] = [Game.from_dict(item) for item in read_json(self.path, {"games": []}).get("games", [])]

    def save(self) -> None:
        write_json(self.path, {"games": [asdict(game) for game in self.games]})

    def get(self, game_id: str) -> Optional[Game]:
        return next((game for game in self.games if game.id == game_id), None)

    def add(self, info: GameInfo, output_dir: Path, to_intel: bool) -> Game:
        source = os.path.abspath(info.folder)
        output = os.path.abspath(output_dir)
        for game in self.games:
            if os.path.normcase(game.source_dir) == os.path.normcase(source):
                raise LauncherError(f"This folder is already in the library as {game.title}.")
            if os.path.normcase(game.output_dir) == os.path.normcase(output):
                raise LauncherError(f"{output} is already the output folder of {game.title}.")
        if is_inside(Path(output), Path(source)) or is_inside(Path(source), Path(output)):
            raise LauncherError("The output folder must be outside the game folder (and the other way round).")
        settings = GameSettings(to_intel=to_intel, module_dirs=";".join(info.suggested_module_dirs))
        game = Game(uuid.uuid4().hex[:12], info.title, info.title_id, info.version, source,
                    os.path.relpath(info.executable, info.folder), output, settings)
        self.games.append(game)
        self.save()
        return game

    def remove(self, game: Game, delete_output: bool) -> None:
        if delete_output and game.output.exists():
            if not (game.output / MARKER_FILE).is_file():
                raise LauncherError(f"{game.output} was not created by the launcher, so it was left in place.")
            backup_saves(game, "before-remove")  # saves outlive the folder; they stay in data/backups
            remove_tree(game.output, game.output.parent)
        self.games = [item for item in self.games if item.id != game.id]
        self.save()


def cpu_is_intel() -> bool:
    identifier = os.environ.get("PROCESSOR_IDENTIFIER", "")
    if not identifier:
        import platform
        identifier = platform.processor()
    return "GenuineIntel" in identifier


def list_gpus() -> list[str]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                                capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and "Basic" not in line]


# ---------------------------------------------------------------------------
# Conversion


def run_streaming(command: list, log: Log, cwd: Optional[Path] = None, env: Optional[dict] = None,
                  cancel: Optional[threading.Event] = None) -> int:
    command = [str(part) for part in command]
    if env is not None and not os.path.isabs(command[0]):
        # Windows resolves a bare program name with the launcher's PATH, not the child's; resolve it here.
        command[0] = shutil.which(command[0], path=env.get("PATH")) or command[0]
    log("> " + subprocess.list2cmdline(command), "cmd")
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    assert process.stdout is not None
    with process.stdout:
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            log(line, "error" if line.startswith("FAIL") else "out")
            if cancel is not None and cancel.is_set():
                process.terminate()
    return process.wait()


def conversion_status(game: Game, install: Optional[Install]) -> tuple[str, str]:
    """(text, level) where level is ok, warn or idle."""
    if not game.converted or not (game.output / OUTPUT_EXE).is_file():
        return "Not converted yet", "idle"
    if install is not None and game.converted.get("relinker_sha") != install.sha:
        if game.converted.get("install") == install.name:
            return f"{install.name} was updated since this conversion: re-convert", "warn"
        return f"Converted with {game.converted.get('install', 'another version')}: re-convert for {install.name}", "warn"
    if game.converted.get("options") != game.settings.relink_options():
        return "Conversion settings changed: re-convert", "warn"
    return "Ready to play", "ok"


def stage_inputs(game: Game, log: Log) -> Path:
    """Copies or unwraps the executable and modules into <output>/_input, which the relinker reads."""
    staging = game.output / STAGING_DIR
    remove_tree(staging, game.output)
    staging.mkdir(parents=True)

    def stage(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if file_kind(source) == "self":
            target.write_bytes(unwrap_fake_self(source.read_bytes()))
            log(f"Unwrapped fake-signed SELF: {source.relative_to(game.source).as_posix()}", "info")
        else:
            link_or_copy(source, target)

    executable = game.source / game.executable
    staged_executable = staging / executable.name
    stage(executable, staged_executable)
    for name in ("icon0.png", "param.json"):
        if (game.source / "sce_sys" / name).is_file():
            stage(game.source / "sce_sys" / name, staging / "sce_sys" / name)
    for path in module_files(game):
        stage(path, staging / path.relative_to(game.source))
    for name in MODULE_DIR_NAMES:
        if (game.source / name).is_dir():
            (staging / name).mkdir(exist_ok=True)
    if not any((staging / name).is_dir() for name in MODULE_DIR_NAMES):
        (staging / "sce_module").mkdir()  # the relinker requires a module folder, even an empty one
    for relative in game.settings.module_dir_list():
        (staging / relative).mkdir(parents=True, exist_ok=True)
    return staged_executable


def mirror_app0(game: Game, log: Log, cancel: Optional[threading.Event] = None) -> None:
    """Fills <output>/app0 with hard links to the game's files, leaving out the modules being converted."""
    app0 = game.output / "app0"
    remove_tree(app0, game.output)
    skip = {os.path.normcase(os.path.abspath(path)) for path in module_files(game)}
    linked = copied = 0
    for root, _, files in os.walk(game.source, onerror=_raise):
        if cancel is not None and cancel.is_set():
            raise LauncherError("Cancelled")
        relative = Path(root).relative_to(game.source)
        os.makedirs(_long(app0 / relative), exist_ok=True)
        for name in files:
            source = Path(root) / name
            if os.path.normcase(os.path.abspath(source)) in skip:
                continue
            if link_or_copy(source, app0 / relative / name):
                linked += 1
            else:
                copied += 1
    log(f"Game files mirrored into app0: {linked} linked" + (f", {copied} copied" if copied else ""), "info")


def copy_libs(install: Install, game: Game, log: Log) -> None:
    libs = game.output / "libs"
    remove_tree(libs, game.output)
    shutil.copytree(install.libs, libs)
    log(f"System libraries copied from {install.name}: {len(list(libs.glob('*.prx')))} libraries", "info")


def convert(game: Game, install: Install, log: Log, cancel: Optional[threading.Event] = None) -> None:
    if not game.source.is_dir():
        raise LauncherError(f"The game folder is missing: {game.source}")
    out = game.output
    out.mkdir(parents=True, exist_ok=True)
    marker = read_json(out / MARKER_FILE, None)
    if marker is None and any(out.iterdir()):
        raise LauncherError(f"{out} already contains files that the launcher did not create. Pick an empty output folder.")
    write_json(out / MARKER_FILE, {"game": game.id, "source": game.source_dir})
    log(f"Converting {game.title} with {install.name} ({install.label})", "info")
    staged = stage_inputs(game, log)
    mirror_app0(game, log, cancel)
    copy_libs(install, game, log)
    for stale in out.glob("eboot*.registry.json"):
        stale.unlink()
    options = game.settings.relink_options()
    command = [install.relinker, "--windows", "--registry"]
    if options["to_intel"]:
        command.append("--to-intel")
    if options["diagnostics"]:
        command.append("--windows-diagnostics")
    if options["unused_filter"]:
        command.append(f"unused-filter={options['unused_filter']}")
    for relative in options["module_dirs"]:
        command += ["--module-dir", relative]
    command += [staged, out / OUTPUT_EXE]
    code = run_streaming(command, log, cwd=out, cancel=cancel)
    if code != 0:
        game.converted = None
        raise LauncherError(f"The relinker failed (exit code {code}); its message is in the log above.")
    game.converted = {"install": install.name, "relinker_sha": install.sha, "options": options,
                      "at": time.strftime("%Y-%m-%d %H:%M")}
    game.audit = None  # it described the previous conversion
    log(f"Converted: {out / OUTPUT_EXE}", "ok")
    if game.restore_on_convert:
        game.restore_on_convert = False
        saves = saves_dir(game)
        backups = list_backups(game)
        if backups and not (saves.is_dir() and any(saves.iterdir())):
            restore_backup(game, backups[0].path)
            log(f"Saves restored from the backup of {backups[0].created} (brought over from the other PC)", "ok")


def audit(game: Game, source_repo: Path, log: Log) -> dict:
    script = source_repo / "tools" / "import_audit.py"
    if not script.is_file():
        raise LauncherError(f"The compatibility check needs the AnyPS5 source checkout ({script} is missing).")
    out = game.output
    main_registry = out / "eboot.registry.json"
    if not main_registry.is_file():
        raise LauncherError("Convert the game first: the check reads the files the conversion writes.")
    registries = [main_registry] + sorted(path for path in out.glob("eboot.*.registry.json"))
    command = [sys.executable, script, *registries, "--libs", out / "libs", "--json", out / AUDIT_JSON]
    for name in MODULE_DIR_NAMES + tuple(game.settings.module_dir_list()):
        if (out / STAGING_DIR / name).is_dir():
            command += ["--modules", out / STAGING_DIR / name]
    previous = read_json(out / AUDIT_JSON, None)
    before = {(record["nid"], record.get("library")): (record.get("class"), record.get("name"))
              for record in (previous.get("imports", []) if isinstance(previous, dict) else [])}
    (out / AUDIT_JSON).unlink(missing_ok=True)
    code = run_streaming(command, log, cwd=source_repo)
    result = read_json(out / AUDIT_JSON, None)
    if code == 2 or result is None:
        raise LauncherError("The compatibility check could not run; see the log.")
    counts = result["unique_by_class"]
    absent = [record for record in result.get("imports", []) if record.get("class") == "absent"]
    # A weak import the loader cannot resolve becomes null instead of stopping the start, so only the others block it.
    required = [record for record in absent if not record.get("weak")]
    optional = len(absent) - len(required)
    summary = {"implemented": counts.get("implemented", 0), "stub": counts.get("stub", 0),
               "absent": max(0, counts.get("absent", 0) - optional), "optional_missing": optional,
               "module": counts.get("module", 0), "missing_libraries": len(result.get("missing_libraries", {})),
               "absent_examples": [f"{record.get('name') or record['nid']} ({record.get('library') or 'unnamed library'})"
                                   for record in required[:3]],
               "at": time.strftime("%Y-%m-%d %H:%M")}
    # Functions that were missing or unfinished at the previous check and are implemented now.
    improved = []
    for record in result.get("imports", []):
        old_class, old_name = before.get((record["nid"], record.get("library")), (None, None))
        if record.get("class") == "implemented" and old_class in ("absent", "stub"):
            improved.append(old_name or record.get("name") or record["nid"])
    summary["improved"], summary["improved_count"] = improved[:20], len(improved)
    if improved:
        log(f"Since the previous check, {len(improved)} function{'s' if len(improved) != 1 else ''} this game uses "
            f"became available: {', '.join(improved[:8])}{'…' if len(improved) > 8 else ''}", "ok")
    game.audit = summary
    record_audit_history(game, summary)
    verdict, level = audit_verdict(summary)
    log(verdict, level)
    return summary


def record_audit_history(game: Game, summary: dict) -> None:
    """Keeps one entry per check; a repeat with the same build and the same counts only refreshes the date."""
    converted = game.converted or {}
    entry = {key: summary[key] for key in ("implemented", "stub", "absent", "missing_libraries", "at")}
    entry.update(install=converted.get("install", ""), build=converted.get("relinker_sha", ""))
    history = game.audit_history
    same = ("implemented", "stub", "absent", "missing_libraries", "build")
    if history and all(history[-1].get(key) == entry[key] for key in same):
        history[-1]["at"] = entry["at"]
    else:
        history.append(entry)
    del history[:-HISTORY_KEPT]


def implemented_share(entry: dict) -> float:
    total = entry["implemented"] + entry["stub"] + entry["absent"] + entry.get("optional_missing", 0)
    return 100 * entry["implemented"] / total if total else 100.0


def history_trend(game: Game) -> Optional[str]:
    """One line on how the missing-function count moved since the first recorded check."""
    history = game.audit_history
    if len(history) < 2:
        return None
    first, last = history[0], history[-1]
    if first["absent"] == last["absent"]:
        return f"Missing functions unchanged at {last['absent']} over {len(history)} checks since {first['at'][:10]}"
    direction = "down" if last["absent"] < first["absent"] else "up"
    return f"Missing functions {direction} from {first['absent']} to {last['absent']} since {first['at'][:10]}"


def audit_verdict(summary: dict) -> tuple[str, str]:
    optional = summary.get("optional_missing", 0)
    total = summary["implemented"] + summary["stub"] + summary["absent"] + optional
    share = implemented_share(summary)
    optional_note = (f"; {optional} optional function{'s' if optional != 1 else ''} it can run without "
                     f"{'are' if optional != 1 else 'is'} missing") if optional else ""
    if summary["absent"] or summary["missing_libraries"]:
        examples = summary.get("absent_examples") or []
        return (f"Will not start yet: AnyPS5 lacks {summary['absent']} system functions and {summary['missing_libraries']} "
                f"libraries this game needs ({share:.0f}% of imports implemented)"
                + (f", e.g. {', '.join(examples)}" if examples else "")), "error"
    if summary["stub"]:
        return (f"Should start, may stop when it calls one of {summary['stub']} unfinished functions ({share:.0f}% implemented)"
                + optional_note), "warn"
    if optional:
        return f"Should start: every function it needs is implemented ({share:.0f}% of imports){optional_note}", "ok"
    return f"Every system function it imports is implemented ({total} imports)", "ok"


# ---------------------------------------------------------------------------
# Playing

EXIT_CODE_HINTS = {
    0xC0000139: "Windows could not find a function the game imports (entry point not found): AnyPS5 is missing a "
                "system function this game uses. The FAIL line above names it.",
    0xC000001D: "The game hit a CPU instruction your processor lacks. Turn on 'Intel CPU fix' and re-convert.",
    0xC0000005: "The game crashed with an access violation. Try 'Store GPU results at each flip', or update AnyPS5.",
    0xC0000409: "The game aborted. The lines just above usually name the reason.",
}
OUTPUT_HINTS = [
    (re.compile(r"unresolved ELF import (\S+) from (\S+)"),
     "AnyPS5 does not have the PS5 system function {0} from {1} yet, so this game cannot start. No setting or "
     "re-convert changes that; it needs AnyPS5 to implement the function (Update AnyPS5 picks it up once it lands)."),
    (re.compile(r"create direct memory backing"),
     "Windows ran out of commit memory for the game. Enlarge the page file (Settings > System > About > Advanced system "
     "settings > Performance > Advanced > Virtual memory) or close other programs."),
    (re.compile(r"(\S+) not implemented"),
     "The game called a PS5 system function AnyPS5 does not support yet ({0}). Update AnyPS5 and re-convert; "
     "if it remains, this game is not supported yet."),
    (re.compile(r"error 193"), "Windows tried to load a PS5 module as a DLL. Re-convert so libs/ holds only AnyPS5 libraries."),
    (re.compile(r"error 126"), "A file the libraries need is missing from libs/. Re-convert the game."),
    (re.compile(r"param\.json not found"), "app0/sce_sys/param.json is missing. Re-convert the game."),
    (re.compile(r"ANYPS5_GPU|no usable (Vulkan )?device", re.IGNORECASE),
     "No usable GPU matched. Set GPU to Automatic in the game settings, and update your graphics driver."),
    (re.compile(r"ANYPS5_SYSTEM_FONTS|sceFontOpenFontSet", re.IGNORECASE),
     "The game wants the console's system fonts. Settings > Fonts > Download free fonts fetches the substitutes AnyPS5 uses."),
]


def hint_for_exit(code: int, settings: GameSettings) -> Optional[str]:
    code &= 0xFFFFFFFF
    if code == 0xC000001D and settings.to_intel:
        return "The game hit a CPU instruction your processor lacks, even with the Intel CPU fix. Update AnyPS5."
    return EXIT_CODE_HINTS.get(code)


def game_environment(game: Game) -> dict:
    env = dict(os.environ)
    settings = game.settings
    if settings.gpu:
        env["ANYPS5_GPU"] = settings.gpu
    if settings.language != DEFAULT_LANGUAGE:
        env["ANYPS5_LANGUAGE"] = str(settings.language)
    if settings.store_at_flip:
        env["APS5_STORE_AT_FLIP"] = "1"
    if settings.thread_priority in ("high", "rt"):
        env["APS5_THREAD_PRIORITY"] = settings.thread_priority
    if INPUT_FILE.is_file() and not (game.output / "anyps5-input.ini").is_file():
        env["ANYPS5_INPUT_CONFIG"] = str(INPUT_FILE)
    fonts = [path for path in FONTS_DIR.glob("*") if path.suffix.lower() in (".ttf", ".otf", ".ttc")] if FONTS_DIR.is_dir() else []
    if fonts and not (game.output / "anyps5-fonts").is_dir():
        env["ANYPS5_SYSTEM_FONTS"] = str(FONTS_DIR)
    for pair in settings.env.split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if key.strip():
                env[key.strip()] = value.strip()
    return env


# Runtime trace lines carry their source location, e.g. "[src/Open.cpp:139 sceKernelOpen] path=/app0/...". Games print
# thousands (one per file opened, thread started...), so the log shows them only on request and counts them otherwise.
TRACE_LINE = re.compile(r"^\[[\w./\\-]+\.(?:cpp|cc|c|hpp|h):\d+ ([\w:~]+)\]")


def trace_name(line: str) -> Optional[str]:
    match = TRACE_LINE.match(line)
    return match.group(1) if match else None


class OutputFilter:
    """Turns game output into log lines: tags runtime traces, adds each hint once, and folds the loader's list of
    searched libraries."""

    def __init__(self):
        self.hinted: set[str] = set()
        self.folding = False
        self.folded = 0

    def feed(self, line: str) -> list[tuple[str, str]]:
        lines = []
        if self.folding:
            if line.startswith("  "):
                self.folded += 1
                return lines
            lines += self.flush()
        lines.append((line, "error" if line.startswith("FAIL") else "trace" if TRACE_LINE.match(line) else "out"))
        if line.strip() == "Searched libraries:":
            self.folding, self.folded = True, 0
        for pattern, hint in OUTPUT_HINTS:
            match = pattern.search(line)
            if match and hint not in self.hinted:
                self.hinted.add(hint)
                lines.append(("Hint: " + hint.format(*match.groups()), "hint"))
        return lines

    def flush(self) -> list[tuple[str, str]]:
        if not self.folding:
            return []
        self.folding = False
        return [(f"  ({self.folded} files; the full list is in {RUN_LOG})", "cmd")]


class GameProcess:
    """A running game: output goes to the log and to <output>/anyps5-last-run.log."""

    def __init__(self, game: Game, log: Log, on_exit: Callable[[int], None],
                 on_frame: Optional[Callable[[float, tuple], None]] = None):
        executable = game.output / OUTPUT_EXE
        if not executable.is_file():
            raise LauncherError("Convert the game before playing it.")
        self.game = game
        self.log = log
        self.on_exit = on_exit
        self.on_frame = on_frame
        self.output = OutputFilter()
        self.tracker = PerformanceTracker()
        self.performance: Optional[dict] = None
        self.stopped = False
        self.elapsed = 0
        self.exit_code: Optional[int] = None
        env = game_environment(game)
        changed = {key: value for key, value in env.items() if os.environ.get(key) != value}
        log(f"Starting {game.title}" + (f" with {', '.join(f'{k}={v}' for k, v in changed.items())}" if changed else ""), "info")
        self.started = time.monotonic()
        self.process = subprocess.Popen([str(executable)], cwd=game.output, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        threading.Thread(target=self._pump, daemon=True).start()
        if os.name == "nt":
            threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        """Polls the game window: the frame rate AnyPS5 writes into its title, and the one-off F11 for fullscreen."""
        pending = self.game.settings.fullscreen and self._fullscreen_key_bound()
        attempts = 0
        while self.process.poll() is None:
            try:
                window = find_window(self.process.pid)
                reading = parse_fps_title(window_title(window)) if window else None
                if reading:
                    self.tracker.add(time.monotonic(), *reading)
                    if self.on_frame:
                        self.on_frame(reading[0], client_origin(window))
                    if pending:
                        if foreground_window() != window:
                            bring_to_front(window)
                            attempts += 1
                        if foreground_window() == window and press_key(VK_F11, SCAN_F11):
                            pending = False
                            self.log("Switched to fullscreen; F11 in the game toggles it back", "info")
                        elif attempts >= 15:
                            pending = False
                            self.log("Hint: Fullscreen skipped because the game window was not in front. "
                                     "Press F11 in the game.", "hint")
            except OSError:
                pass
            time.sleep(1)

    def _fullscreen_key_bound(self) -> bool:
        custom = read_input_file().get("ToggleFullscreen")
        if custom and not any(binding.replace(" ", "").upper() == "KEY:F11" for binding in custom):
            self.log("Hint: Start in fullscreen needs F11 bound to ToggleFullscreen (Controls). Skipped.", "hint")
            return False
        return True

    def _pump(self) -> None:
        assert self.process.stdout is not None
        with open(self.game.output / RUN_LOG, "w", encoding="utf-8", errors="replace") as record:
            for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                record.write(line + "\n")
                for text, tag in self.output.feed(line):
                    self.log(text, tag)
        for text, tag in self.output.flush():
            self.log(text, tag)
        code = self.process.wait()
        elapsed = time.monotonic() - self.started
        self.elapsed = int(elapsed)
        self.exit_code = code
        self.performance = self.tracker.summary()
        if self.stopped:
            self.log(f"{self.game.title} stopped after {elapsed:.0f} s", "info")
        else:
            self.log(f"{self.game.title} exited with code {code} (0x{code & 0xFFFFFFFF:08X}) after {elapsed:.0f} s",
                     "ok" if code == 0 else "error")
            hint = hint_for_exit(code, self.game.settings)
            # The output hint already named the missing function; the exit-code hint would only repeat it.
            if hint and not (code & 0xFFFFFFFF == 0xC0000139 and self.output.hinted):
                self.log("Hint: " + hint, "hint")
        self.on_exit(code)

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.running:
            self.stopped = True
            self.process.terminate()


# ---------------------------------------------------------------------------
# Controls file


def read_input_file(path: Optional[Path] = None) -> dict[str, list[str]]:
    path = path or INPUT_FILE
    bindings: dict[str, list[str]] = {}
    if not path.is_file():
        return bindings
    names = {name.lower(): name for name in INPUT_ACTIONS}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = re.split(r"[#;]", line, maxsplit=1)[0].strip()
        if "=" not in line:
            continue
        action, binding = (part.strip() for part in line.split("=", 1))
        if action.lower() in names:
            bindings.setdefault(names[action.lower()], []).append(binding)
    return bindings


def validate_binding(binding: str) -> Optional[str]:
    """Returns an error message, or None when the binding has the documented Type:Value form."""
    kind, _, value = binding.partition(":")
    kind, value = kind.strip().upper(), value.strip()
    if not value:
        return f"'{binding}' needs the form KEY:Name, MOUSE:Button or WHEEL:Up/Down"
    if kind == "KEY":
        return None
    if kind == "MOUSE":
        return None if value.title() in MOUSE_BUTTONS or value.upper() in MOUSE_BUTTONS else f"Mouse button must be one of {', '.join(MOUSE_BUTTONS)}"
    if kind == "WHEEL":
        return None if value.lower() in ("up", "down") else "Wheel must be Up or Down"
    return f"'{binding}': the type must be KEY, MOUSE or WHEEL"


def write_input_file(bindings: dict[str, list[str]], path: Optional[Path] = None, keep_empty: bool = False) -> None:
    """Writes the bindings; with none, removes the file, unless `keep_empty` (a game's own file that should use the
    built-in bindings rather than fall back to the shared layout)."""
    path = path or INPUT_FILE
    lines = ["# Written by the AnyPS5 launcher. Actions not listed keep their built-in bindings.",
             "# Format: Action = KEY:Name | MOUSE:Left/Middle/Right/X1/X2 | WHEEL:Up/Down"]
    for action in INPUT_ACTIONS:
        for binding in bindings.get(action, []):
            lines.append(f"{action} = {binding}")
    if len(lines) == 2 and not keep_empty:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Updating AnyPS5


def toolchain_env(toolchain: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(toolchain) + os.pathsep + env.get("PATH", "")
    if GIT_CA_BUNDLE.is_file():
        env.setdefault("SSL_CERT_FILE", str(GIT_CA_BUNDLE))
    return env


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise LauncherError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def check_source_updates(repo: Path) -> int:
    """Number of commits upstream has that the local checkout lacks."""
    git_output(repo, "fetch", "--quiet")
    return int(git_output(repo, "rev-list", "--count", "HEAD..@{u}") or 0)


def install_from_build(repo: Path, toolchain: Path, destination: Path, log: Log) -> None:
    build = repo / "build"
    relinker = build / "core" / "relinker" / "relinker.exe"
    libraries = sorted((build / "core" / "libs" / "libs").glob("*.prx"))
    expected = {f"{path.name}.prx" for path in (repo / "core" / "libs" / "prx").iterdir() if path.is_dir()}
    missing = expected - {path.name for path in libraries}
    if not relinker.is_file() or missing:
        raise LauncherError("The build is incomplete" + (f"; missing libraries: {', '.join(sorted(missing))}" if missing else ""))
    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        remove_tree(staging, destination.parent)
    (staging / "libs").mkdir(parents=True)
    shutil.copy2(relinker, staging / "relinker.exe")
    for library in libraries:
        shutil.copy2(library, staging / "libs" / library.name)
    for name in RUNTIME_DLLS:
        shutil.copy2(toolchain / name, staging / "libs" / name)
    for document in (repo / "docs" / "user").glob("*.md"):
        shutil.copy2(document, staging / document.name)
    revision = git_output(repo, "log", "-1", "--format=%h %ci")
    (staging / "VERSION.txt").write_text(f"AnyPS5 main {revision} (built locally)\n", encoding="utf-8")
    # Swap whole folders, so files a running conversion holds open are never rewritten in place.
    previous = destination.with_name(destination.name + ".old")
    if previous.exists():
        if destination.exists():
            remove_tree(previous, destination.parent)
        else:
            # An earlier swap stopped half-way, so the .old folder is the install: put it back before going on.
            replace_when_free(previous, destination)
    if destination.exists():
        replace_when_free(destination, previous)
    try:
        replace_when_free(staging, destination)
    except OSError as error:
        if previous.exists() and not destination.exists():
            try:
                replace_when_free(previous, destination)
            except OSError:
                pass
        kept = "The previous AnyPS5 is still installed" if destination.exists() else f"The previous AnyPS5 is kept in {previous}"
        raise LauncherError(f"Windows would not let the launcher move the new build into {destination} ({error.strerror}). "
                            f"A program such as antivirus or OneDrive was probably still reading the new files. {kept}; "
                            "run the update again.") from error
    if previous.exists():
        remove_tree(previous, destination.parent)
    log(f"Installed {len(libraries)} libraries and the relinker into {destination} (main {revision})", "ok")


def replace_when_free(source: Path, target: Path, attempts: int = 40, delay: float = 0.5) -> None:
    """os.replace, retried while another program (antivirus, OneDrive, an Explorer window) still holds a file inside."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def update_from_source(repo: Path, toolchain: Path, destination: Path, log: Log,
                       cancel: Optional[threading.Event] = None, keep_edits: bool = True) -> None:
    """Pulls, builds and installs AnyPS5. Edits made to the checkout are carried over (`keep_edits`), or set aside in
    git's stash so the official code is built. Everything logged is also written to <data>/anyps5-last-update.log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / UPDATE_LOG, "w", encoding="utf-8", errors="replace") as record:
        def logged(text: str, tag: str = "info") -> None:
            record.write(f"{text}\n")
            record.flush()
            log(text, tag)

        build_and_install(repo, toolchain, destination, logged, cancel, keep_edits)


def build_and_install(repo: Path, toolchain: Path, destination: Path, log: Log,
                      cancel: Optional[threading.Event], keep_edits: bool) -> None:
    if not (repo / ".git").exists():
        raise LauncherError(f"No AnyPS5 source checkout at {repo}. Set its location in Settings.")
    if not (toolchain / "g++.exe").is_file():
        raise LauncherError(f"No MinGW toolchain at {toolchain}. Set its location in Settings.")
    env = toolchain_env(toolchain)
    before = git_output(repo, "rev-parse", "--short", "HEAD")
    build = repo / "build"
    if not keep_edits:
        set_edits_aside(repo, env, log)
    pull_with_local_edits(repo, env, log, cancel)
    steps = [["git", "-C", repo, "submodule", "update", "--init", "--recursive"]]
    if not (build / "build.ninja").is_file():
        steps.append(["cmake", "-S", repo, "-B", build, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
                      "-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++"])
    steps += [["cmake", "--build", build, "--parallel"],
              ["cmake", "--build", build, "--target", "libs", "--parallel"]]
    for step in steps:
        if cancel is not None and cancel.is_set():
            raise LauncherError("Cancelled")
        code = run_streaming(step, log, cwd=repo, env=env, cancel=cancel)
        if code != 0 and step[1] == "--build" and not (cancel is not None and cancel.is_set()):
            # A build that runs out of memory, or meets a file another program holds, fails without a code error;
            # the files that did compile are kept, so one slower retry costs little.
            jobs = max(1, (os.cpu_count() or 2) // 2)
            log(f"The build failed. Trying once more with {jobs} parallel jobs, in case it ran out of memory.", "warn")
            code = run_streaming([*step, str(jobs)], log, cwd=repo, env=env, cancel=cancel)
        if code != 0:
            edited = local_edits(repo)
            raise LauncherError(f"'{' '.join(str(part) for part in step[:4])}' failed; see the log "
                                f"({DATA_DIR / UPDATE_LOG})."
                                + (f" The AnyPS5 source has your own edits ({', '.join(edited[:3])}); they may be the "
                                   "cause." if edited and step[1] == "--build" else ""))
    after = git_output(repo, "rev-parse", "--short", "HEAD")
    log(f"Source {before} -> {after}" if before != after else f"Source already at {after}; rebuilt anyway", "info")
    install_from_build(repo, toolchain, destination, log)


def local_edits(repo: Path) -> list[str]:
    """Tracked files changed in the checkout but not committed."""
    # Porcelain lines are "XY path"; the output is stripped, so the first line may have lost its leading space.
    return [line.split(None, 1)[1].split(" -> ")[-1] for line in
            git_output(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if len(line.split(None, 1)) == 2]


def set_edits_aside(repo: Path, env: dict, log: Log) -> Optional[str]:
    """Moves uncommitted edits into git's stash (never deleting them) so the official code can be built."""
    edited = local_edits(repo)
    if not edited:
        return None
    label = f"anyps5-launcher: edits set aside to build the official code, {time.strftime('%Y-%m-%d %H:%M')}"
    if run_streaming(["git", "-C", repo, "stash", "push", "-m", label], log, cwd=repo, env=env) != 0:
        raise LauncherError("Could not set your edits to the AnyPS5 source aside; nothing was changed.")
    log(f"Your edits to {len(edited)} file{'s' if len(edited) != 1 else ''} are kept in git's stash as \"{label}\". "
        f"'git -C {repo} stash list' shows them and 'git stash apply' brings them back.", "info")
    return label


def pull_with_local_edits(repo: Path, env: dict, log: Log, cancel: Optional[threading.Event] = None) -> None:
    """Fast-forwards the checkout without losing edits made to it: they are stashed, the pull runs, and they are put
    back. When they no longer apply to the new code they stay in the stash (named after the launcher) and the clean
    new version is built; nothing is ever discarded."""
    edited = local_edits(repo)
    label = ""
    if edited:
        label = f"anyps5-launcher: edits set aside before the update of {time.strftime('%Y-%m-%d %H:%M')}"
        log(f"The AnyPS5 source has your own edits to {len(edited)} file{'s' if len(edited) != 1 else ''} "
            f"({', '.join(edited[:4])}{'…' if len(edited) > 4 else ''}). Setting them aside for the update.", "info")
        if run_streaming(["git", "-C", repo, "stash", "push", "-m", label], log, cwd=repo, env=env) != 0:
            raise LauncherError("Could not set your edits to the AnyPS5 source aside; nothing was changed.")
    pulled = run_streaming(["git", "-C", repo, "pull", "--ff-only"], log, cwd=repo, env=env, cancel=cancel) == 0
    if label:
        stash = next((line.split(":", 1)[0] for line in git_output(repo, "stash", "list").splitlines() if label in line), None)
        if stash is None:
            raise LauncherError(f"Your edits were stashed as '{label}', but that stash entry is gone; check 'git stash list'.")
        if run_streaming(["git", "-C", repo, "stash", "apply", stash], log, cwd=repo, env=env) == 0:
            git_output(repo, "stash", "drop", stash)
            log("Your edits were put back on top of the new AnyPS5 source.", "ok")
        else:
            # The edits clash with the new code. They stay safe in the stash; the tree goes back to the new version.
            git_output(repo, "reset", "--hard", "HEAD")
            log(f"Your edits no longer fit the new AnyPS5 code, so this update is built without them. They are kept "
                f"in git's stash as \"{label}\": 'git -C {repo} stash list' shows it, and 'git stash apply' brings it "
                "back to merge by hand.", "warn")
    if not pulled:
        raise LauncherError("Getting the new AnyPS5 source failed; see the log." + (" Your edits are back in place." if label else ""))


def download_latest_release(destination_root: Path, log: Log) -> Path:
    request = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "anyps5-launcher"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release["tag_name"]
    if not re.fullmatch(r"v[0-9A-Za-z._-]+", tag):
        raise LauncherError(f"Unexpected release tag {tag!r}")
    destination = destination_root / f"AnyPS5-{tag}"
    if (destination / "relinker.exe").is_file():
        log(f"{tag} is already installed in {destination}", "ok")
        return destination
    assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
    wanted = (f"relinker-{tag}.exe", f"prx-windows-{tag}.zip")
    if not all(name in assets for name in wanted):
        raise LauncherError(f"Release {tag} has no Windows build")
    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        remove_tree(staging, destination_root)
    staging.mkdir(parents=True)
    for name in wanted:
        log(f"Downloading {name}", "info")
        urllib.request.urlretrieve(assets[name], staging / name)  # noqa: S310 - fixed https host
    with zipfile.ZipFile(staging / wanted[1]) as archive:
        for member in archive.namelist():
            if not member.startswith("libs/") or ".." in Path(member).parts:
                raise LauncherError(f"Unexpected file in the release archive: {member}")
        archive.extractall(staging)
    (staging / wanted[1]).unlink()
    os.replace(staging / wanted[0], staging / "relinker.exe")
    (staging / "VERSION.txt").write_text(f"AnyPS5 {tag} (official release)\n", encoding="utf-8")
    os.replace(staging, destination)
    log(f"Installed release {tag} into {destination}", "ok")
    return destination


# ---------------------------------------------------------------------------
# Launcher settings


@dataclass
class LauncherSettings:
    install: str = LATEST_INSTALL_NAME
    source_dir: str = DEFAULT_SOURCE_DIR
    toolchain_dir: str = DEFAULT_TOOLCHAIN_DIR
    geometry: str = ""
    selected: str = ""
    theme: str = "system"           # system, light or dark
    view: str = "grid"              # grid or list
    sort: str = "name"              # name, recent or played
    auto_backup: bool = True        # back up saves before each session
    notify_updates: bool = True
    last_update_check: float = 0.0
    notified_revision: str = ""
    fps_overlay: bool = False       # always-on-top frame-rate counter while a game runs
    screenshot_key: bool = True     # a key saves a screenshot while a game runs
    screenshot_hotkey: str = "F9"   # one of HOTKEYS
    home: str = "console"           # console (the PS5-style home screen) or classic
    discord_app_id: str = ""        # Discord status needs an application ID from discord.com/developers
    start_menu_folder: bool = False  # keep "AnyPS5 Games" in the Start menu in sync with the library
    desktop_folder: bool = False     # the same folder on the desktop

    @staticmethod
    def load(path: Optional[Path] = None) -> "LauncherSettings":
        value = read_json(path or SETTINGS_FILE, {})
        return LauncherSettings(**{key: item for key, item in value.items() if key in LauncherSettings.__dataclass_fields__})

    def save(self, path: Optional[Path] = None) -> None:
        write_json(path or SETTINGS_FILE, asdict(self))


# ---------------------------------------------------------------------------
# Library helpers and playtime


def find_game(library: Library, key: str) -> Optional[Game]:
    """A game by launcher id or by title id (PPSA…), as desktop shortcuts and the command line pass them."""
    key = key.strip()
    return library.get(key) or next((game for game in library.games if game.title_id and game.title_id.lower() == key.lower()), None)


def outdated_games(library: Library, install: Optional[Install]) -> list[Game]:
    """Converted games whose conversion predates the selected AnyPS5 build or their own settings."""
    return [game for game in library.games if game.converted and conversion_status(game, install)[1] == "warn"]


def will_not_start(game: Game) -> bool:
    return bool(game.audit and (game.audit["absent"] or game.audit["missing_libraries"]))


def record_session(game: Game, seconds: int) -> None:
    seconds = max(0, int(seconds))
    game.playtime += seconds
    game.sessions += 1
    game.last_session = seconds
    game.last_played = time.strftime("%Y-%m-%d %H:%M")


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute" if seconds else "0 min"
    hours, minutes = divmod(seconds // 60, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


# ---------------------------------------------------------------------------
# Save backups

BACKUP_NAME = re.compile(r"^(\d{8}-\d{6})-(manual|auto|before-restore|before-remove|imported|export)\.zip$")
DEDUPED_BACKUPS = ("auto", "export")  # kinds made by the launcher on its own: skipped when nothing changed
BACKUP_MANIFEST = "anyps5-backup.json"


@dataclass
class Backup:
    path: Path
    created: str
    kind: str
    size: int


def saves_dir(game: Game) -> Path:
    return game.output / SAVES_DIR_NAME


def backup_dir(game: Game) -> Path:
    return BACKUPS_DIR / game.id


def _saves_digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in folder.rglob("*") if item.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def list_backups(game: Game) -> list[Backup]:
    folder = backup_dir(game)
    backups = []
    for path in folder.glob("*.zip") if folder.is_dir() else []:
        match = BACKUP_NAME.match(path.name)
        if match:
            stamp = match.group(1)
            created = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
            backups.append(Backup(path, created, match.group(2), path.stat().st_size))
    return sorted(backups, key=lambda backup: backup.path.name, reverse=True)


def backup_saves(game: Game, kind: str = "manual") -> Optional[Backup]:
    """Zips <output>/_sd. Returns None when there is nothing to save, or (for automatic backups) nothing changed."""
    source = saves_dir(game)
    if not source.is_dir() or not any(path.is_file() for path in source.rglob("*")):
        return None
    digest = _saves_digest(source)
    existing = list_backups(game)
    if kind in DEDUPED_BACKUPS and existing:
        try:
            with zipfile.ZipFile(existing[0].path) as archive:
                if json.loads(archive.read(BACKUP_MANIFEST)).get("digest") == digest:
                    return None
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            pass
    folder = backup_dir(game)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.zip"
    while target.exists():
        time.sleep(1)
        target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.zip"
    temporary = target.with_suffix(".tmp")
    manifest = {"title": game.title, "title_id": game.title_id, "created": target.name[:15], "kind": kind, "digest": digest}
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest, indent=2))
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            archive.write(path, f"{SAVES_DIR_NAME}/{path.relative_to(source).as_posix()}")
    os.replace(temporary, target)
    if kind == "auto":
        for old in [backup for backup in list_backups(game) if backup.kind == "auto"][AUTO_BACKUPS_KEPT:]:
            old.path.unlink()
    return next(backup for backup in list_backups(game) if backup.path == target)


def read_backup_manifest(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                parts = Path(name).parts
                if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                    raise LauncherError(f"{path.name} contains an unsafe path: {name}")
                if name != BACKUP_MANIFEST and not name.startswith(SAVES_DIR_NAME + "/"):
                    raise LauncherError(f"{path.name} is not an AnyPS5 launcher save backup")
            return json.loads(archive.read(BACKUP_MANIFEST)) if BACKUP_MANIFEST in names else {}
    except zipfile.BadZipFile as error:
        raise LauncherError(f"{path.name} is not a valid zip file") from error


def restore_backup(game: Game, backup: Path) -> Optional[Backup]:
    """Replaces the game's saves with a backup; the current saves are backed up first and that backup is returned."""
    read_backup_manifest(backup)
    safety = backup_saves(game, "before-restore")
    staging = game.output / (SAVES_DIR_NAME + ".restoring")
    remove_tree(staging, game.output)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(backup) as archive:
        for member in archive.infolist():
            if member.filename == BACKUP_MANIFEST or member.is_dir():
                continue
            target = staging / Path(member.filename).relative_to(SAVES_DIR_NAME)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
    current = saves_dir(game)
    previous = game.output / (SAVES_DIR_NAME + ".old")
    remove_tree(previous, game.output)
    if current.exists():
        os.replace(current, previous)
    os.replace(staging, current)
    remove_tree(previous, game.output)
    return safety


def import_backup(game: Game, source: Path) -> Backup:
    manifest = read_backup_manifest(source)
    if manifest.get("title_id") and game.title_id and manifest["title_id"] != game.title_id:
        raise LauncherError(f"This backup belongs to {manifest.get('title', manifest['title_id'])}, not {game.title}.")
    folder = backup_dir(game)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-imported.zip"
    shutil.copy2(source, target)
    return next(backup for backup in list_backups(game) if backup.path == target)


# ---------------------------------------------------------------------------
# Desktop integration


def pythonw_path() -> str:
    alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "pythonw.exe"
    if alias.is_file():
        return str(alias)  # stable across Store Python updates, unlike the versioned install folder
    sibling = Path(sys.executable).with_name("pythonw.exe")
    return str(sibling if sibling.is_file() else sys.executable)


def desktop_dir() -> Path:
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:  # CSIDL_DESKTOPDIRECTORY
            return Path(buffer.value)
    return Path.home() / "Desktop"


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return cleaned[:80] or "Game"


def _powershell(script: str, env: dict, wait: bool = True) -> None:
    command = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    if wait:
        result = subprocess.run(command, env={**os.environ, **env}, capture_output=True, text=True, timeout=30,
                                creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise LauncherError(result.stderr.strip() or "PowerShell failed")
    else:
        subprocess.Popen(command, env={**os.environ, **env}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


TOAST_SCRIPT = (
    "$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime];"
    "$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime];"
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'><text/><text/></binding></visual></toast>\");"
    "$texts = $xml.GetElementsByTagName('text');"
    "$null = $texts.Item(0).AppendChild($xml.CreateTextNode($env:TOAST_TITLE));"
    "$null = $texts.Item(1).AppendChild($xml.CreateTextNode($env:TOAST_BODY));"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)")


def windows_toast(title: str, message: str, wait: bool = False) -> None:
    """Best-effort Windows notification; does nothing where toasts are unavailable."""
    if os.name != "nt":
        return
    try:
        _powershell(TOAST_SCRIPT, {"TOAST_TITLE": title, "TOAST_BODY": message}, wait=wait)
    except (OSError, subprocess.SubprocessError, LauncherError):
        if wait:
            raise


def upstream_revision(repo: Path) -> str:
    return git_output(repo, "rev-parse", "--short", "@{u}")


class SingleInstance:
    """Lets a second launcher, such as one started from a game's desktop shortcut, hand its request to the open one."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or INSTANCE_FILE
        self.token = uuid.uuid4().hex
        self.server: Optional[socket.socket] = None

    def forward(self, message: dict) -> bool:
        info = read_json(self.path, None)
        if not isinstance(info, dict):
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2) as connection:
                connection.sendall((json.dumps({**message, "token": info["token"]}) + "\n").encode())
                return connection.recv(8).startswith(b"ok")
        except (OSError, KeyError, ValueError, TypeError):
            return False

    def serve(self, handler: Callable[[dict], None]) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.server = server
        write_json(self.path, {"port": server.getsockname()[1], "token": self.token, "pid": os.getpid()})
        threading.Thread(target=self._accept, args=(server, handler), daemon=True).start()

    def _accept(self, server: socket.socket, handler: Callable[[dict], None]) -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(2)
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 4096:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    message = json.loads(data.decode("utf-8"))
                    if not isinstance(message, dict) or message.pop("token", None) != self.token:
                        connection.sendall(b"no\n")
                        continue
                    handler(message)
                    connection.sendall(b"ok\n")
                except (OSError, ValueError):
                    continue

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
            info = read_json(self.path, None)
            if isinstance(info, dict) and info.get("token") == self.token:
                self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Frame rate. AnyPS5's video output writes "<title> | FPS: <2-second average> (<frame number>)" into the window title
# (core/libs/prx/libSceVideoOut/src/DisplayWindow.cpp), so the launcher reads performance without touching the game.

FPS_TITLE = re.compile(r"\|\s*FPS:\s*([0-9]+(?:\.[0-9]+)?)\s*\((\d+)\)\s*$")
PERFORMANCE_WARMUP = 10.0   # seconds after the first frame: loading and first shader compiles would skew the numbers
PERFORMANCE_KEPT = 50


def parse_fps_title(title: str) -> Optional[tuple[float, int]]:
    match = FPS_TITLE.search(title or "")
    return (float(match.group(1)), int(match.group(2))) if match else None


class PerformanceTracker:
    def __init__(self, warmup: float = PERFORMANCE_WARMUP):
        self.warmup = warmup
        self.first: Optional[float] = None
        self.samples: list[tuple[float, float, int]] = []   # (time, fps reading, frame number)

    def add(self, at: float, fps: float, frame: int) -> None:
        if self.first is None:
            self.first = at
        if at - self.first < self.warmup or (self.samples and frame <= self.samples[-1][2]):
            return
        self.samples.append((at, fps, frame))

    def summary(self) -> Optional[dict]:
        """Average, low and high of the runtime's 2-second readings, so the three always agree with each other.

        Readings are taken once a second, so each counts in proportion to the time it was on screen."""
        if len(self.samples) < 3:
            return None
        readings = [fps for _, fps, _ in self.samples[1:]]  # the first reading still spans the warm-up
        return {"avg": round(sum(readings) / len(readings), 1), "low": round(min(readings), 1),
                "high": round(max(readings), 1), "seconds": int(self.samples[-1][0] - self.samples[0][0])}


def record_performance(game: Game, summary: dict) -> None:
    game.performance.append({**summary, "at": time.strftime("%Y-%m-%d %H:%M")})
    del game.performance[:-PERFORMANCE_KEPT]


def performance_text(game: Game) -> Optional[str]:
    if not game.performance:
        return None
    last = game.performance[-1]
    text = f"Last session: {last['avg']} FPS average, lowest {last['low']} (2-second windows, {format_duration(last['seconds'])} measured)"
    if len(game.performance) > 1:
        overall = sum(entry["avg"] for entry in game.performance) / len(game.performance)
        text += f". {len(game.performance)} sessions average {overall:.1f} FPS"
    return text


# ---------------------------------------------------------------------------
# Game windows (Windows): finding the game's window, bringing it to the front and pressing its fullscreen key.

VK_F11, SCAN_F11 = 0x7A, 0x57

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)  # private instance: argtypes set here stay here
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def find_window(pid: int) -> int:
    """The visible, titled top-level window of a process, or 0."""
    found: list[int] = []

    def visit(hwnd, _param):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd) and _user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(visit), 0)
    return found[0] if found else 0


def window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def client_origin(hwnd: int) -> tuple[int, int]:
    """Screen position of the window's drawing area (below the title bar when windowed)."""
    point = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(point))
    return point.x, point.y


def foreground_window() -> int:
    return _user32.GetForegroundWindow() or 0


def bring_to_front(hwnd: int) -> bool:
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(_user32.SetForegroundWindow(hwnd))


def press_key(vk: int, scan: int) -> bool:
    """One press and release, as the keyboard would send it, to the foreground window."""
    keys = (_INPUT * 2)(_INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, 0, 0, 0))),
                        _INPUT(1, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, 2, 0, 0))))  # KEYEVENTF_KEYUP
    return _user32.SendInput(2, keys, ctypes.sizeof(_INPUT)) == 2


# ---------------------------------------------------------------------------
# Shortcut folders: "AnyPS5 Games" in the Start menu and on the desktop, one shortcut per game plus the launcher.

SHORTCUT_FOLDER = "AnyPS5 Games"
# The shortcut list travels in a JSON file, so titles with quotes or $ cannot break the script.
SHORTCUTS_SCRIPT = (
    "$items = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:LNK_FILE | ConvertFrom-Json; "
    "$shell = New-Object -ComObject WScript.Shell; "
    "foreach ($i in $items) { $s = $shell.CreateShortcut($i.path); $s.TargetPath = $i.target; $s.Arguments = $i.args; "
    "$s.WorkingDirectory = $i.dir; $s.IconLocation = $i.icon; $s.Description = $i.desc; $s.Save() }")


def start_menu_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_FOLDER


def desktop_games_dir() -> Path:
    return desktop_dir() / SHORTCUT_FOLDER


def shortcut_spec(game: Game, link: Path) -> dict:
    executable = game.output / OUTPUT_EXE
    return {"path": str(link), "target": pythonw_path(),
            "args": subprocess.list2cmdline([str(LAUNCHER_SCRIPT), "--play", game.id]), "dir": str(LAUNCHER_DIR),
            "icon": f"{executable},0" if executable.is_file() else f"{pythonw_path()},0",
            "desc": f"Play {game.title} with AnyPS5"[:250]}


def create_shortcuts(specs: list[dict]) -> None:
    if not specs:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    batch = DATA_DIR / f"shortcuts-{uuid.uuid4().hex[:8]}.json"
    batch.write_text(json.dumps(specs, ensure_ascii=False), encoding="utf-8")
    try:
        _powershell(SHORTCUTS_SCRIPT, {"LNK_FILE": str(batch)})
    finally:
        batch.unlink(missing_ok=True)


def create_game_shortcut(game: Game, folder: Optional[Path] = None) -> Path:
    link = (folder or desktop_dir()) / f"{safe_filename(game.title)}.lnk"
    create_shortcuts([shortcut_spec(game, link)])
    return link


def sync_shortcut_folder(games: list[Game], folder: Path) -> tuple[int, int]:
    """Makes `folder` hold exactly one shortcut per game plus one for the launcher; returns (written, removed)."""
    folder.mkdir(parents=True, exist_ok=True)
    names: dict[str, int] = {}
    specs = []
    for game in sorted(games, key=lambda item: item.title.lower()):
        name = safe_filename(game.title)
        names[name.lower()] = names.get(name.lower(), 0) + 1
        if names[name.lower()] > 1:
            name = f"{name} ({game.title_id or game.id})"
        specs.append(shortcut_spec(game, folder / f"{name}.lnk"))
    specs.append({"path": str(folder / "AnyPS5 Launcher.lnk"), "target": pythonw_path(),
                  "args": subprocess.list2cmdline([str(LAUNCHER_SCRIPT)]), "dir": str(LAUNCHER_DIR),
                  "icon": f"{pythonw_path()},0", "desc": "Convert and play PS5 games with AnyPS5"})
    wanted = {os.path.normcase(spec["path"]) for spec in specs}
    removed = 0
    for link in folder.glob("*.lnk"):
        if os.path.normcase(str(link)) not in wanted:
            link.unlink()
            removed += 1
    create_shortcuts(specs)
    return len(specs), removed


def remove_shortcut_folder(folder: Path) -> None:
    if folder.is_dir():
        for link in folder.glob("*.lnk"):
            link.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()


# ---------------------------------------------------------------------------
# Controllers. XInput covers Xbox-style pads; PlayStation pads are not XInput devices, so they are only detected here
# (games read them directly through SDL). Button names follow AnyPS5's mapping in libSceVideoOut/src/PadInput.cpp.

XINPUT_BUTTONS = {0x0001: "Up", 0x0002: "Down", 0x0004: "Left", 0x0008: "Right", 0x0010: "Options", 0x0020: "TouchPad",
                  0x0040: "L3", 0x0080: "R3", 0x0100: "L1", 0x0200: "R1", 0x1000: "Cross", 0x2000: "Circle",
                  0x4000: "Square", 0x8000: "Triangle"}


class XInput:
    def __init__(self):
        self.dll = None
        if os.name != "nt":
            return
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue

        class Gamepad(ctypes.Structure):
            _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                        ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                        ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

        class State(ctypes.Structure):
            _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", Gamepad)]

        class Vibration(ctypes.Structure):
            _fields_ = [("wLeftMotorSpeed", wintypes.WORD), ("wRightMotorSpeed", wintypes.WORD)]

        self.State, self.Vibration = State, Vibration

    @property
    def available(self) -> bool:
        return self.dll is not None

    def read(self, index: int) -> Optional[dict]:
        """Buttons pressed (PS names), triggers 0..1 and sticks -1..1 (up is positive), or None when not connected."""
        if self.dll is None:
            return None
        state = self.State()
        if self.dll.XInputGetState(index, ctypes.byref(state)) != 0:
            return None
        pad = state.Gamepad

        def axis(value: int) -> float:
            return max(-1.0, min(1.0, value / 32767))

        return {"buttons": {name for bit, name in XINPUT_BUTTONS.items() if pad.wButtons & bit},
                "l2": pad.bLeftTrigger / 255, "r2": pad.bRightTrigger / 255,
                "lx": axis(pad.sThumbLX), "ly": axis(pad.sThumbLY), "rx": axis(pad.sThumbRX), "ry": axis(pad.sThumbRY)}

    def connected(self) -> list[int]:
        return [index for index in range(4) if self.read(index) is not None]

    def vibrate(self, index: int, left: float, right: float) -> bool:
        if self.dll is None:
            return False
        vibration = self.Vibration(int(max(0.0, min(1.0, left)) * 65535), int(max(0.0, min(1.0, right)) * 65535))
        return self.dll.XInputSetState(index, ctypes.byref(vibration)) == 0


def playstation_controllers() -> list[str]:
    """Names of connected Sony controllers (USB or Bluetooth), from Windows' device list."""
    if os.name != "nt":
        return []
    script = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '054C' -and $_.FriendlyName } | "
              "Select-Object -ExpandProperty FriendlyName | Sort-Object -Unique")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                                text=True, timeout=20, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Problem report: what someone helping with a failing game needs, without the thousands of trace lines.

REPORT_TAGS = ("cmd", "info", "ok", "warn", "error", "hint")


def build_report(game: Game, install: Optional[Install], lines: list[tuple[str, str]], tail: int = 40) -> str:
    settings = game.settings
    defaults = GameSettings()
    changed = {key: value for key, value in asdict(settings).items() if getattr(defaults, key) != value}
    status, _ = conversion_status(game, install)
    report = [f"Game: {game.title} ({game.title_id or 'no title ID'}, version {game.version or '?'})",
              f"AnyPS5: {install.label if install else 'not installed'}",
              f"Launcher status: {status}"]
    if game.audit:
        report.append(f"Compatibility: {audit_verdict(game.audit)[0]}")
    if changed:
        report.append("Settings changed from defaults: " + ", ".join(f"{key}={value}" for key, value in changed.items()))
    if game.performance:
        report.append(performance_text(game) or "")
    if os.environ.get("PROCESSOR_IDENTIFIER"):
        report.append(f"CPU: {os.environ['PROCESSOR_IDENTIFIER']}")
    important = [text for text, tag in lines if tag in ("warn", "error", "hint")]
    if important:
        report += ["", "Problems in the log:"] + [f"  {text}" for text in important[-30:]]
    output = [text for text, tag in lines if tag != "trace"][-tail:]
    if output:
        report += ["", f"Last {len(output)} log lines (runtime traces left out):"] + [f"  {text}" for text in output]
    return "\n".join(report)


# ---------------------------------------------------------------------------
# Scanning a folder or drive for games


@dataclass
class ScanResult:
    folder: Path
    info: Optional[GameInfo] = None
    error: str = ""
    in_library: bool = False


SCAN_SKIP = {"$recycle.bin", "system volume information", "windows", "program files", "program files (x86)",
             "programdata", "appdata", "node_modules", ".git"}


def scan_for_games(root: Path, library: Optional[Library], max_depth: int = 6, cancel: Optional[threading.Event] = None,
                   progress: Optional[Callable[[str], None]] = None) -> list[ScanResult]:
    """Game folders under `root`: an executable at the top and sce_sys/param.json or a module folder beside it.
    Folders the launcher created (the converted games) are skipped."""
    known = {os.path.normcase(os.path.abspath(game.source_dir)) for game in (library.games if library else [])}
    results, root = [], Path(root)
    for current, dirs, files in os.walk(root):
        if cancel is not None and cancel.is_set():
            break
        here = Path(current)
        depth = len(here.relative_to(root).parts)
        names = {name.lower() for name in files}
        if MARKER_FILE.lower() in names:
            dirs[:] = []
            continue
        has_executable = any(candidate in names for candidate in EXECUTABLE_CANDIDATES)
        if has_executable and ((here / "sce_sys" / "param.json").is_file() or any((here / d).is_dir() for d in MODULE_DIR_NAMES)):
            dirs[:] = []  # never look inside a game for more games
            if progress:
                progress(str(here))
            result = ScanResult(here, in_library=os.path.normcase(os.path.abspath(here)) in known)
            try:
                result.info = inspect_game_folder(here)
            except (LauncherError, OSError) as error:
                result.error = str(error)
            results.append(result)
            continue
        dirs[:] = [d for d in dirs if not d.startswith((".", "$")) and d.lower() not in SCAN_SKIP
                   and not d.lower().endswith("-anyps5") and depth < max_depth]
    # Games first, by title; folders that could not be read after them.
    return sorted(results, key=lambda result: (result.info is None,
                                               result.info.title.lower() if result.info else result.folder.name.lower()))


# ---------------------------------------------------------------------------
# Fonts: the openly licensed (SIL OFL) substitutes AnyPS5 loads when a game opens the console's system font set
# (docs/user/USAGE.md, "System fonts"), downloaded one file at a time from Google's Noto repositories.

_NOTO = "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts"
_WEIGHTS = ("Light", "Regular", "Medium", "Bold")
FONT_FILES = (
    [(f"NotoSans-{weight}.ttf", f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-{weight}.ttf") for weight in _WEIGHTS]
    + [(f"NotoSans-{style}.ttf", f"{_NOTO}/NotoSans/hinted/ttf/NotoSans-{style}.ttf")
       for style in ("LightItalic", "Italic", "MediumItalic", "BoldItalic")]
    + [(f"NotoSansMono-{weight}.ttf", f"{_NOTO}/NotoSansMono/hinted/ttf/NotoSansMono-{weight}.ttf") for weight in _WEIGHTS]
    + [(f"NotoSansThai-{weight}.ttf", f"{_NOTO}/NotoSansThai/hinted/ttf/NotoSansThai-{weight}.ttf") for weight in _WEIGHTS])
CJK_FONT_FILES = [(f"NotoSansCJK-{weight}.ttc",
                   f"https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/NotoSansCJK-{weight}.ttc")
                  for weight in _WEIGHTS]
FONT_MAGICS = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")


def installed_fonts() -> tuple[int, int]:
    """(basic substitutes present, CJK substitutes present)."""
    def present(files):
        return sum(1 for name, _ in files if (FONTS_DIR / name).is_file() and (FONTS_DIR / name).stat().st_size > 1000)
    return present(FONT_FILES), present(CJK_FONT_FILES)


def download_fonts(include_cjk: bool, log: Log, cancel: Optional[threading.Event] = None) -> int:
    """Downloads the missing font files; returns how many were fetched."""
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    wanted = list(FONT_FILES) + (CJK_FONT_FILES if include_cjk else [])
    fetched = 0
    for index, (name, url) in enumerate(wanted, 1):
        target = FONTS_DIR / name
        if target.is_file() and target.stat().st_size > 1000:
            continue
        if cancel is not None and cancel.is_set():
            raise LauncherError("Cancelled")
        log(f"[{index}/{len(wanted)}] {name}", "info")
        temporary = target.with_name(target.name + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "anyps5-launcher"})
        with urllib.request.urlopen(request, timeout=60) as response, open(temporary, "wb") as output:
            shutil.copyfileobj(response, output)
        with open(temporary, "rb") as check:
            magic = check.read(4)
        if magic not in FONT_MAGICS:  # e.g. an HTML error page
            temporary.unlink()
            raise LauncherError(f"{name} did not download as a font file")
        os.replace(temporary, target)
        fetched += 1
    return fetched


# ---------------------------------------------------------------------------
# Per-game controls: AnyPS5 reads anyps5-input.ini beside the game's executable, and the launcher only points the
# runtime at the shared file when a game has none of its own (game_environment).


def game_input_file(game: Game) -> Path:
    return game.output / "anyps5-input.ini"


def has_own_controls(game: Game) -> bool:
    return game_input_file(game).is_file()


# ---------------------------------------------------------------------------
# Organizing


def parse_tags(text: str) -> list[str]:
    seen, tags = set(), []
    for part in re.split(r"[,;]", text):
        tag = " ".join(part.split())
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def all_tags(library: Library) -> list[str]:
    return sorted({tag for game in library.games for tag in game.tags}, key=str.lower)


def matches_search(game: Game, query: str) -> bool:
    query = query.strip().lower()
    return not query or any(query in text.lower() for text in (game.title, game.title_id or "", game.notes, *game.tags))


# ---------------------------------------------------------------------------
# Storage. Hard-linked game files share their data with the original folder, so only files with a single link
# count as space the converted folder uses.


def folder_bytes(path: Path, unique_only: bool = False) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for current, _, files in os.walk(path):
        for name in files:
            try:
                info = os.stat(os.path.join(current, name))
            except OSError:
                continue
            if not unique_only or info.st_nlink <= 1:
                total += info.st_size
    return total


def storage_report(game: Game) -> dict:
    out = game.output
    report = {"libraries": folder_bytes(out / "libs"), "shader_cache": folder_bytes(out / "shader_cache"),
              "saves": folder_bytes(out / SAVES_DIR_NAME), "downloads": folder_bytes(out / "download0"),
              "game_files": folder_bytes(out / "app0", unique_only=True) + folder_bytes(out / STAGING_DIR, unique_only=True),
              "backups": folder_bytes(backup_dir(game))}
    in_output = folder_bytes(out, unique_only=True)
    report["other"] = max(0, in_output - sum(value for key, value in report.items() if key != "backups"))
    report["total"] = in_output + report["backups"]
    return report


def clear_shader_cache(game: Game) -> int:
    cache = game.output / "shader_cache"
    freed = folder_bytes(cache)
    remove_tree(cache, game.output)
    return freed


def prune_auto_backups(game: Game, keep: int = 3) -> int:
    freed = 0
    for backup in [item for item in list_backups(game) if item.kind == "auto"][keep:]:
        freed += backup.size
        backup.path.unlink(missing_ok=True)
    return freed


# ---------------------------------------------------------------------------
# What's new in AnyPS5. Upstream merges pull requests whose description line follows "kind(area): title".

CHANGE_KINDS = {"feat": "New", "fix": "Fixed", "perf": "Faster"}
CONVENTIONAL_TYPES = {"feat", "fix", "perf", "build", "ci", "docs", "test", "tests", "refactor", "chore", "style", "revert"}
CONVENTIONAL = re.compile(r"^(\w+)(?:\(([^)]*)\))?!?:\s*(.+)$")


@dataclass
class Change:
    commit: str
    kind: str        # New, Fixed, Faster or Other
    area: str        # e.g. libkernel, agc, relinker
    title: str
    pr: str = ""


def parse_change(commit: str, subject: str, body: str) -> Change:
    pr, text = "", subject.strip()
    merge = re.match(r"Merge pull request #(\d+)", text)
    if merge:
        pr = merge.group(1)
        text = next((line.strip() for line in body.splitlines() if line.strip()), text)
    match = CONVENTIONAL.match(text)
    if match and match.group(1).lower() in CONVENTIONAL_TYPES:
        kind, area, title = CHANGE_KINDS.get(match.group(1).lower(), "Other"), (match.group(2) or "").strip(), match.group(3).strip()
    else:
        loose = re.match(r"^([\w.\-/ ]{2,40}):\s+(.+)$", text)  # "libkernel: implement shm_unlink"
        area, title = (loose.group(1), loose.group(2)) if loose else ("", text)
        kind = ("New" if re.match(r"(?i)(add|implement|support|export)\b", title) else
                "Fixed" if re.match(r"(?i)(fix|correct|handle|avoid|prevent|reject)\b", title) else "Other")
    return Change(commit, kind, area, title[:1].upper() + title[1:], pr)


def pending_changes(repo: Path) -> list[Change]:
    """Upstream changes the local checkout does not have yet, newest first."""
    git_output(repo, "fetch", "--quiet")
    raw = git_output(repo, "log", "--first-parent", "HEAD..@{u}", "--format=%h%x1f%s%x1f%b%x1e")
    changes = []
    for entry in raw.split("\x1e"):
        parts = entry.strip("\r\n").split("\x1f")
        if len(parts) >= 2 and parts[0].strip():
            changes.append(parse_change(parts[0].strip(), parts[1], parts[2] if len(parts) > 2 else ""))
    return changes


def audit_function_names(game: Game) -> set[str]:
    """Names of the functions the last compatibility check found missing or unfinished for this game."""
    data = read_json(game.output / AUDIT_JSON, None)
    records = data.get("imports", []) if isinstance(data, dict) else []
    return {record["name"].replace("_nid_postfix", "") for record in records
            if record.get("class") in ("absent", "stub") and record.get("name")}


def changes_for_games(changes: list[Change], games: list[Game]) -> dict[str, list[tuple[Change, str]]]:
    """Per game id: the changes that name one of its missing or unfinished functions, or its title ID."""
    result: dict[str, list[tuple[Change, str]]] = {}
    for game in games:
        names = {name for name in audit_function_names(game) if len(name) > 5 and not name.startswith("sceKernelUnknown")}
        for change in changes:
            text = f"{change.area} {change.title}"
            reason = None
            if game.title_id and game.title_id.lower() in text.lower():
                reason = f"mentions {game.title_id}"
            else:
                hit = next((name for name in sorted(names) if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text)), None)
                if hit:
                    reason = f"works on {hit}, which it uses"
            if reason:
                result.setdefault(game.id, []).append((change, reason))
    return result


# ---------------------------------------------------------------------------
# Screenshots: the game window's picture, read from the screen (which also covers Vulkan and fullscreen games),
# saved as PNG under Pictures\AnyPS5\<game>, with a small copy in .thumbs for the gallery.

SCREENSHOT_THUMB_WIDTH = 240


def pictures_dir() -> Path:
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buffer) == 0:  # CSIDL_MYPICTURES
            return Path(buffer.value) / "AnyPS5"
    return Path.home() / "Pictures" / "AnyPS5"


def screenshots_dir(game: Game) -> Path:
    return pictures_dir() / safe_filename(game.title)


def encode_png(width: int, height: int, rgb: bytes, alpha: bool = False) -> bytes:
    """8-bit RGB (or RGBA with `alpha`) PNG."""
    stride = width * (4 if alpha else 3)
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type: none
        raw += rgb[row * stride:(row + 1) * stride]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def bgra_to_rgb(raw: bytes, width: int, height: int) -> bytes:
    """Windows' 32-bit BGRA pixels to packed RGB, with slice copies rather than a per-pixel Python loop."""
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
    return bytes(rgb)


def downscale(rgb: bytes, width: int, height: int, step: int) -> tuple[int, int, bytes]:
    stride, rows = width * 3, []
    for row in range(0, height, step):
        line = rgb[row * stride:(row + 1) * stride]
        small = bytearray(len(line[0::3 * step]) * 3)
        small[0::3], small[1::3], small[2::3] = line[0::3 * step], line[1::3 * step], line[2::3 * step]
        rows.append(bytes(small))
    return len(rows[0]) // 3, len(rows), b"".join(rows)


def capture_client(hwnd: int) -> tuple[int, int, bytes]:
    x, y = client_origin(hwnd)
    rect = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise LauncherError("The game window has no picture to capture (is it minimised?)")
    screen = _user32.GetDC(None)
    memory = _gdi32.CreateCompatibleDC(screen)
    bitmap = _gdi32.CreateCompatibleBitmap(screen, width, height)
    try:
        _gdi32.SelectObject(memory, bitmap)
        _gdi32.BitBlt(memory, 0, 0, width, height, screen, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0) + bytes(4)  # top-down 32 bpp
        info = ctypes.create_string_buffer(header, len(header))
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(memory, bitmap, 0, height, pixels, info, 0):
            raise LauncherError("Windows did not return the screen pixels")
        return width, height, bgra_to_rgb(pixels.raw, width, height)
    finally:
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory)
        _user32.ReleaseDC(None, screen)


def take_screenshot(game: Game, pid: int) -> Path:
    window = find_window(pid)
    if not window:
        raise LauncherError("The game window was not found")
    width, height, rgb = capture_client(window)
    folder = screenshots_dir(game)
    (folder / ".thumbs").mkdir(parents=True, exist_ok=True)
    stem = f"{safe_filename(game.title)} {time.strftime('%Y-%m-%d %H-%M-%S')}"
    target, number = folder / f"{stem}.png", 2
    while target.exists():
        target, number = folder / f"{stem} ({number}).png", number + 1
    target.write_bytes(encode_png(width, height, rgb))
    step = max(1, math.ceil(width / SCREENSHOT_THUMB_WIDTH))
    thumb_width, thumb_height, thumb = downscale(rgb, width, height, step)
    (folder / ".thumbs" / target.name).write_bytes(encode_png(thumb_width, thumb_height, thumb))
    return target


def list_screenshots(game: Game) -> list[Path]:
    folder = screenshots_dir(game)
    shots = [path for path in folder.glob("*.png") if path.is_file()] if folder.is_dir() else []
    return sorted(shots, key=lambda path: path.stat().st_mtime, reverse=True)


def screenshot_thumbnail(shot: Path) -> Path:
    return shot.parent / ".thumbs" / shot.name


def delete_screenshot(shot: Path) -> None:
    shot.unlink(missing_ok=True)
    screenshot_thumbnail(shot).unlink(missing_ok=True)


if os.name == "nt":
    _gdi32 = ctypes.WinDLL("gdi32")
    _kernel32 = ctypes.WinDLL("kernel32")
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                              ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                 ctypes.c_void_p, wintypes.UINT]
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


# Keys offered for screenshots: (modifiers, virtual key). F12 is not among them: Windows reserves it for the system
# debugger and refuses it as a hot key (error 1409).
HOTKEYS = {"F9": (0, 0x78), "F8": (0, 0x77), "F10": (0, 0x79), "Ctrl+F12": (0x2, 0x7B), "Print Screen": (0, 0x2C)}
DEFAULT_HOTKEY = "F9"


class Hotkey:
    """A system-wide key while a game runs (screenshots). Windows ties a hot key and its messages to the thread
    that registered it, so registration, the message loop and the release all happen on one thread."""

    def __init__(self, name: str, callback: Callable[[], None]):
        self.name = name if name in HOTKEYS else DEFAULT_HOTKEY
        self.modifiers, self.vk = HOTKEYS[self.name]
        self.callback = callback
        self.thread_id = 0
        self.error = ""
        self.ready = threading.Event()

    def start(self) -> bool:
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(2)
        return not self.error

    def _run(self) -> None:
        self.thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, 1, self.modifiers | 0x4000, self.vk):  # MOD_NOREPEAT
            self.error = f"{self.name} is already taken by another program"
            self.ready.set()
            return
        self.ready.set()
        message = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == 0x0312:  # WM_HOTKEY
                try:
                    self.callback()
                except Exception:  # noqa: BLE001 - a failed capture must not end the key's thread
                    pass
        _user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT


# ---------------------------------------------------------------------------
# Discord status ("Rich Presence") over Discord's local named pipe. Discord shows the application's name as the
# activity, so it needs an application ID the user creates at discord.com/developers; without one, or without Discord
# running, nothing happens.


class DiscordPresence:
    def __init__(self, app_id: str, connect: Optional[Callable[[], object]] = None):
        self.app_id = app_id.strip()
        self.connect = connect or self._open_pipe
        self.pipe = None
        self.last_error = ""
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _open_pipe():
        for index in range(10):
            try:
                return open(rf"\\.\pipe\discord-ipc-{index}", "r+b", buffering=0)
            except OSError:
                continue
        return None

    @staticmethod
    def frame(op: int, payload: dict) -> bytes:
        data = json.dumps(payload).encode("utf-8")
        return struct.pack("<II", op, len(data)) + data

    def _read(self) -> dict:
        header = self.pipe.read(8)
        if len(header) < 8:
            raise OSError("Discord closed the connection")
        _, length = struct.unpack("<II", header)
        return json.loads(self.pipe.read(length) or b"{}")

    def _send(self, op: int, payload: dict) -> dict:
        self.pipe.write(self.frame(op, payload))
        return self._read()

    def _ready(self) -> bool:
        if self.pipe is not None:
            return True
        self.pipe = self.connect()
        if self.pipe is None:
            self.last_error = "Discord is not running"
            return False
        reply = self._send(0, {"v": 1, "client_id": self.app_id})
        if reply.get("evt") != "READY":
            self.last_error = (reply.get("data") or {}).get("message", "Discord refused the connection")
            self._drop()
            return False
        return True

    def _drop(self):
        try:
            if self.pipe is not None:
                self.pipe.close()
        except OSError:
            pass
        self.pipe = None

    def _run(self) -> None:
        while True:
            activity = self.jobs.get()
            if activity is None:
                self._drop()
                return
            try:
                if self._ready():
                    args = {"pid": os.getpid(), **({"activity": activity} if activity else {})}
                    reply = self._send(1, {"cmd": "SET_ACTIVITY", "args": args, "nonce": uuid.uuid4().hex})
                    if reply.get("evt") == "ERROR":
                        self.last_error = (reply.get("data") or {}).get("message", "Discord rejected the status")
            except (OSError, ValueError) as error:
                self.last_error = str(error)
                self._drop()

    def show(self, title: str, started: float) -> None:
        if self.app_id:
            self.jobs.put({"details": title[:120], "state": "On PC with AnyPS5", "timestamps": {"start": int(started)}})

    def clear(self) -> None:
        if self.app_id:
            self.jobs.put({})

    def close(self) -> None:
        self.jobs.put(None)


# ---------------------------------------------------------------------------
# Moving to another PC: one zip with the library, portable settings, controls and save backups (screenshots optional).

EXPORT_FORMAT = "anyps5-launcher-export"
PORTABLE_SETTINGS = ("theme", "view", "sort", "auto_backup", "notify_updates", "fps_overlay", "screenshot_key",
                     "screenshot_hotkey", "start_menu_folder", "desktop_folder", "discord_app_id", "home")


def export_bundle(library: Library, settings: "LauncherSettings", target: Path, include_screenshots: bool, log: Log) -> dict:
    counts = {"games": len(library.games), "backups": 0, "controls": 0, "screenshots": 0}
    for game in library.games:
        try:
            if backup_saves(game, "export"):
                log(f"Saved the current saves of {game.title} for the move", "info")
        except OSError as error:
            log(f"Could not back up the saves of {game.title}: {error}", "warn")
    temporary = target.with_name(target.name + ".part")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"format": EXPORT_FORMAT, "version": 1, "games": len(library.games),
                                                      "exported": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
        archive.writestr("library.json", json.dumps({"games": [asdict(game) for game in library.games]}, indent=2,
                                                    ensure_ascii=False))
        archive.writestr("settings.json", json.dumps({key: getattr(settings, key) for key in PORTABLE_SETTINGS}, indent=2))
        if INPUT_FILE.is_file():
            archive.write(INPUT_FILE, "controls/shared.ini")
            counts["controls"] += 1
        for game in library.games:
            if has_own_controls(game):
                archive.write(game_input_file(game), f"controls/{game.id}.ini")
                counts["controls"] += 1
            for backup in list_backups(game):
                archive.write(backup.path, f"backups/{game.id}/{backup.path.name}")
                counts["backups"] += 1
            if include_screenshots:
                for shot in list_screenshots(game):
                    archive.write(shot, f"screenshots/{game.id}/{shot.name}")
                    counts["screenshots"] += 1
    os.replace(temporary, target)
    return counts


def read_bundle(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                parts = Path(name).parts
                if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                    raise LauncherError(f"{path.name} contains an unsafe path: {name}")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != EXPORT_FORMAT:
                raise LauncherError(f"{path.name} is not an AnyPS5 launcher export")
            games = [Game.from_dict(item) for item in json.loads(archive.read("library.json")).get("games", [])]
            settings = json.loads(archive.read("settings.json")) if "settings.json" in archive.namelist() else {}
            return {"manifest": manifest, "games": games, "settings": settings, "names": archive.namelist()}
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise LauncherError(f"{path.name} is not a readable launcher export ({error})") from error


def find_moved_games(games: list[Game], root: Path, cancel: Optional[threading.Event] = None) -> dict[str, Path]:
    """Game folders under `root` for games whose folder does not exist here, matched by title ID."""
    missing = {game.title_id.lower(): game.id for game in games if game.title_id and not Path(game.source_dir).is_dir()}
    found: dict[str, Path] = {}
    for result in scan_for_games(root, None, cancel=cancel):
        if result.info and result.info.title_id.lower() in missing:
            found.setdefault(missing[result.info.title_id.lower()], result.folder)
    return found


def import_bundle(path: Path, library: Library, settings: "LauncherSettings", relocations: dict[str, Path], log: Log) -> list[Game]:
    bundle = read_bundle(path)
    names = set(bundle["names"])
    known_ids = {game.id for game in library.games}
    known_sources = {os.path.normcase(os.path.abspath(game.source_dir)) for game in library.games}
    added = []
    with zipfile.ZipFile(path) as archive:
        for game in bundle["games"]:
            if game.id in known_ids:
                log(f"Skipped {game.title}: it is already in this library", "info")
                continue
            source = relocations.get(game.id)
            if source is not None:
                game.source_dir = os.path.abspath(source)
                game.output_dir = os.path.abspath(default_output_dir(Path(source)))
            if os.path.normcase(os.path.abspath(game.source_dir)) in known_sources:
                log(f"Skipped {game.title}: its folder is already in this library", "info")
                continue
            game.converted = None  # the converted copy stays on the other PC
            backups = sorted(name for name in names if name.startswith(f"backups/{game.id}/") and name.endswith(".zip"))
            if backups:
                folder = backup_dir(game)
                folder.mkdir(parents=True, exist_ok=True)
                for name in backups:
                    target = folder / Path(name).name
                    if not target.exists() and BACKUP_NAME.match(target.name):
                        target.write_bytes(archive.read(name))
                game.restore_on_convert = True
            controls = f"controls/{game.id}.ini"
            if controls in names:
                try:
                    out = game.output
                    out.mkdir(parents=True, exist_ok=True)
                    if (out / MARKER_FILE).is_file() or not any(out.iterdir()):
                        write_json(out / MARKER_FILE, {"game": game.id, "source": game.source_dir})
                        game_input_file(game).write_bytes(archive.read(controls))
                except OSError as error:
                    log(f"{game.title}: its own controls were not restored ({error})", "warn")
            for name in (item for item in names if item.startswith(f"screenshots/{game.id}/") and item.endswith(".png")):
                target = screenshots_dir(game) / Path(name).name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
            library.games.append(game)
            known_ids.add(game.id)
            added.append(game)
        if "controls/shared.ini" in names and not INPUT_FILE.exists():
            INPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            INPUT_FILE.write_bytes(archive.read("controls/shared.ini"))
    for key, value in bundle["settings"].items():
        if key in PORTABLE_SETTINGS and hasattr(settings, key):
            setattr(settings, key, value)
    library.save()
    settings.save()
    return added


def relocate_game(game: Game, folder: Path) -> GameInfo:
    """Points a game at a new folder (moved drive, another PC). It then needs converting again."""
    info = inspect_game_folder(folder)
    game.source_dir = os.path.abspath(info.folder)
    game.executable = os.path.relpath(info.executable, info.folder)
    game.converted = None
    return info
