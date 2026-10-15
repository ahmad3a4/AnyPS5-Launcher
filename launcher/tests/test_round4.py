import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

# Never touch the real launcher data: point it at a throwaway folder before the engine is imported.
os.environ.setdefault("ANYPS5_LAUNCHER_DATA", tempfile.mkdtemp(prefix="anyps5-launcher-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anyps5core as core  # noqa: E402
from test_anyps5core import make_self  # noqa: E402


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Enough of PNG for the launcher's own files: 8-bit RGB, filter 0."""
    width, height = struct.unpack(">II", data[16:24])
    position, idat = 8, b""
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        if kind == b"IDAT":
            idat += data[position + 8:position + 8 + length]
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3
    return width, height, b"".join(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)] for row in range(height))


class WhatsNewTests(unittest.TestCase):
    def test_parse_changes(self):
        merge = core.parse_change("abc", "Merge pull request #3481 from someone/branch",
                                  "fix(libkernel_sys): check fchdir search permission on Linux\n\nlonger text")
        self.assertEqual((merge.kind, merge.area, merge.title, merge.pr),
                         ("Fixed", "libkernel_sys", "Check fchdir search permission on Linux", "3481"))
        self.assertEqual(core.parse_change("d", "feat(tools): mark empty functions", "").kind, "New")
        self.assertEqual(core.parse_change("d", "build: remove accidental gitlink", "").kind, "Other")
        loose = core.parse_change("e", "libkernel: implement shm_unlink (PPSA99010 vkQuake)", "")
        self.assertEqual((loose.kind, loose.area), ("New", "libkernel"))

    def test_changes_for_games(self):
        with tempfile.TemporaryDirectory() as temp:
            game = core.Game("g1", "Test", "PPSA23827", "", str(Path(temp) / "s"), "eboot.bin", str(Path(temp) / "o"))
            game.output.mkdir()
            (game.output / core.AUDIT_JSON).write_text(json.dumps({"imports": [
                {"nid": "a", "class": "stub", "name": "sceAudioInOpen"},
                {"nid": "b", "class": "stub", "name": "sceKernelUnknown23"},
                {"nid": "c", "class": "implemented", "name": None}]}))
            changes = [core.parse_change("1", "Merge pull request #1 from x/y", "feat(libSceAudioIn): implement sceAudioInOpen"),
                       core.parse_change("2", "Merge pull request #2 from x/y", "fix(exports): export the calls PPSA23827 imports"),
                       core.parse_change("3", "Merge pull request #3 from x/y", "fix(agc): unrelated change"),
                       core.parse_change("4", "Merge pull request #4 from x/y", "feat: sceAudioInOpenEx support")]
            relevant = core.changes_for_games(changes, [game])["g1"]
            self.assertEqual([(change.pr, reason) for change, reason in relevant],
                             [("1", "works on sceAudioInOpen, which it uses"), ("2", "mentions PPSA23827")],
                             "a longer name that merely starts the same does not count")

    def test_audit_reports_newly_available_functions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = core.Game("g1", "Test", "PPSA1", "", str(root / "s"), "eboot.bin", str(root / "o"))
            (game.output / core.STAGING_DIR).mkdir(parents=True)
            (game.output / "eboot.registry.json").write_text("{}")
            (root / "tools").mkdir()
            (root / "tools" / "import_audit.py").write_text("")
            (game.output / core.AUDIT_JSON).write_text(json.dumps({"imports": [
                {"nid": "n1", "library": "libkernel.prx", "class": "stub", "name": "sceKernelFoo"},
                {"nid": "n2", "library": "libkernel.prx", "class": "absent", "name": None}]}))

            def fake_run(command, log, cwd=None, env=None, cancel=None):
                (game.output / core.AUDIT_JSON).write_text(json.dumps({
                    "unique_by_class": {"implemented": 2, "stub": 0, "absent": 0, "module": 0}, "missing_libraries": {},
                    "imports": [{"nid": "n1", "library": "libkernel.prx", "class": "implemented", "name": None},
                                {"nid": "n2", "library": "libkernel.prx", "class": "implemented", "name": None}]}))
                return 0

            lines = []
            with mock.patch.object(core, "run_streaming", fake_run):
                summary = core.audit(game, root, lambda text, tag: lines.append(text))
            self.assertEqual((summary["improved_count"], summary["improved"]), (2, ["sceKernelFoo", "n2"]))
            self.assertTrue(any("2 functions this game uses became available" in line for line in lines))

    def test_audit_does_not_block_on_weak_imports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = core.Game("g1", "Test", "PPSA1", "", str(root / "s"), "eboot.bin", str(root / "o"))
            (game.output / core.STAGING_DIR).mkdir(parents=True)
            (game.output / "eboot.registry.json").write_text("{}")
            (root / "tools").mkdir()
            (root / "tools" / "import_audit.py").write_text("")

            def fake_run(command, log, cwd=None, env=None, cancel=None):
                (game.output / core.AUDIT_JSON).write_text(json.dumps({
                    "unique_by_class": {"implemented": 3, "stub": 0, "absent": 2, "module": 0}, "missing_libraries": {},
                    "imports": [{"nid": "n1", "library": "libkernel.prx", "class": "implemented", "name": None},
                                {"nid": "n2", "library": "libSceLncUtil.prx", "class": "absent", "name": None, "weak": True},
                                {"nid": "n3", "library": "libSceFoo.prx", "class": "absent", "name": None}]}))
                return 1

            lines = []
            with mock.patch.object(core, "run_streaming", fake_run):
                summary = core.audit(game, root, lambda text, tag: lines.append((text, tag)))
            self.assertEqual((summary["absent"], summary["optional_missing"]), (1, 1))
            self.assertEqual(summary["absent_examples"], ["n3 (libSceFoo.prx)"], "only required functions are named")
            self.assertEqual(lines[-1][1], "error")

            def fake_run_weak_only(command, log, cwd=None, env=None, cancel=None):
                (game.output / core.AUDIT_JSON).write_text(json.dumps({
                    "unique_by_class": {"implemented": 3, "stub": 0, "absent": 1, "module": 0}, "missing_libraries": {},
                    "imports": [{"nid": "n2", "library": "libSceLncUtil.prx", "class": "absent", "name": None, "weak": True}]}))
                return 1

            lines.clear()
            with mock.patch.object(core, "run_streaming", fake_run_weak_only):
                summary = core.audit(game, root, lambda text, tag: lines.append((text, tag)))
            self.assertEqual((summary["absent"], summary["optional_missing"]), (0, 1))
            self.assertEqual(lines[-1][1], "ok")
            self.assertNotIn("Will not start", lines[-1][0])


@unittest.skipUnless(os.name == "nt", "Windows screen capture")
class ScreenshotTests(unittest.TestCase):
    def test_png_helpers(self):
        bgra = bytes([10, 20, 30, 255]) * 6  # 3x2 pixels of B=10 G=20 R=30
        rgb = core.bgra_to_rgb(bgra, 3, 2)
        self.assertEqual(rgb, bytes([30, 20, 10]) * 6)
        width, height, pixels = decode_png(core.encode_png(3, 2, rgb))
        self.assertEqual((width, height, pixels), (3, 2, rgb))
        small_width, small_height, small = core.downscale(bytes(range(4 * 4 * 3)), 4, 4, 2)
        self.assertEqual((small_width, small_height), (2, 2))
        self.assertEqual(small[:3], bytes([0, 1, 2]))

    def test_capture_a_real_window(self):
        import tkinter as tk
        root = tk.Tk()
        root.title("capture test")
        root.geometry("200x120+40+40")
        root.configure(background="#20c040")
        root.attributes("-topmost", True)
        root.update()
        time.sleep(0.6)
        root.update()
        try:
            with tempfile.TemporaryDirectory() as temp, mock.patch.object(core, "pictures_dir", lambda: Path(temp)):
                game = core.Game("g", "Capture: Test?", "", "", "s", "eboot.bin", "o")
                shot = core.take_screenshot(game, os.getpid())
                width, height, pixels = decode_png(shot.read_bytes())
                self.assertEqual(shot.parent.name, "Capture Test")
                self.assertTrue(core.screenshot_thumbnail(shot).is_file())
                self.assertGreaterEqual(width, 150)
                middle = ((height // 2) * width + width // 2) * 3
                self.assertEqual(tuple(pixels[middle:middle + 3]), (0x20, 0xC0, 0x40))
                self.assertEqual(core.list_screenshots(game), [shot])
                core.delete_screenshot(shot)
                self.assertEqual(core.list_screenshots(game), [])
        finally:
            root.destroy()

    def test_hotkey_registers_and_stops(self):
        with mock.patch.dict(core.HOTKEYS, {"F24": (0, 0x87)}):  # F24: free on practically every machine
            key = core.Hotkey("F24", lambda: None)
            self.assertTrue(key.start(), key.error)
            key.stop()
        self.assertEqual(core.Hotkey("F12", lambda: None).name, core.DEFAULT_HOTKEY, "F12 is never offered")


class DiscordTests(unittest.TestCase):
    class FakePipe(io.RawIOBase):
        def __init__(self, replies):
            self.replies = [core.DiscordPresence.frame(1, reply) for reply in replies]
            self.written = []
            self.buffer = b""

        def write(self, data):
            self.written.append(data)
            if self.replies:
                self.buffer += self.replies.pop(0)
            return len(data)

        def read(self, size=-1):
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            return data

    def sent(self, pipe):
        return [json.loads(frame[8:]) for frame in pipe.written]

    def wait(self, presence):
        for _ in range(100):
            if presence.jobs.empty():
                time.sleep(0.05)
                return
            time.sleep(0.02)

    def test_handshake_then_activity_then_clear(self):
        pipe = self.FakePipe([{"evt": "READY"}, {"evt": None}, {"evt": None}])
        presence = core.DiscordPresence("123456", connect=lambda: pipe)
        presence.show("DOOM", 1_760_000_000)
        presence.clear()
        self.wait(presence)
        handshake, activity, clear = self.sent(pipe)
        self.assertEqual(handshake, {"v": 1, "client_id": "123456"})
        self.assertEqual(activity["cmd"], "SET_ACTIVITY")
        self.assertEqual(activity["args"]["activity"]["details"], "DOOM")
        self.assertEqual(activity["args"]["activity"]["timestamps"]["start"], 1_760_000_000)
        self.assertNotIn("activity", clear["args"], "no activity clears the status")
        presence.close()

    def test_without_id_or_discord_nothing_happens(self):
        pipe = self.FakePipe([])
        quiet = core.DiscordPresence("", connect=lambda: pipe)
        quiet.show("DOOM", 0)
        self.wait(quiet)
        self.assertEqual(pipe.written, [])
        absent = core.DiscordPresence("1", connect=lambda: None)
        absent.show("DOOM", 0)
        self.wait(absent)
        self.assertEqual(absent.last_error, "Discord is not running")

    def test_refused_handshake(self):
        pipe = self.FakePipe([{"evt": "ERROR", "data": {"message": "Invalid Client ID"}}])
        presence = core.DiscordPresence("999", connect=lambda: pipe)
        presence.show("DOOM", 0)
        self.wait(presence)
        self.assertEqual(presence.last_error, "Invalid Client ID")


class DefaultPathTests(unittest.TestCase):
    def test_defaults_follow_the_data_folder_at_call_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.multiple(core, LIBRARY_FILE=root / "library.json", SETTINGS_FILE=root / "settings.json",
                                     INPUT_FILE=root / "input.ini"):
                library = core.Library()
                self.assertEqual((library.path, library.games), (root / "library.json", []))
                library.save()
                core.LauncherSettings(theme="dark").save()
                self.assertEqual(core.LauncherSettings.load().theme, "dark")
                core.write_input_file({"Cross": ["KEY:F"]})
                self.assertEqual(core.read_input_file(), {"Cross": ["KEY:F"]})
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["input.ini", "library.json", "settings.json"])


class MoveTests(unittest.TestCase):
    def test_export_and_import_on_another_pc(self):
        with tempfile.TemporaryDirectory() as old_pc, tempfile.TemporaryDirectory() as new_pc:
            old, new = Path(old_pc), Path(new_pc)
            source = old / "games" / "PPSA00001"
            (source / "sce_sys").mkdir(parents=True)
            (source / "eboot.bin").write_bytes(make_self([b"x"]))
            (source / "sce_sys" / "param.json").write_text(json.dumps({"titleId": "PPSA00001", "localizedParameters": {
                "defaultLanguage": "en-US", "en-US": {"titleName": "Moved Game"}}}))
            with mock.patch.multiple(core, BACKUPS_DIR=old / "backups", INPUT_FILE=old / "input.ini",
                                     pictures_dir=lambda: old / "pictures"):
                library = core.Library(old / "library.json")
                game = library.add(core.inspect_game_folder(source), old / "games" / "PPSA00001-anyps5", to_intel=True)
                game.playtime, game.tags, game.notes, game.favorite = 3600, ["rpg"], "Chapter 2", True
                core.saves_dir(game).mkdir(parents=True)
                (core.saves_dir(game) / "slot.bin").write_bytes(b"progress")
                (game.output / core.MARKER_FILE).write_text("{}")
                core.write_input_file({"Cross": ["KEY:J"]}, core.game_input_file(game))
                core.write_input_file({"Circle": ["KEY:K"]})
                settings = core.LauncherSettings(theme="dark", fps_overlay=True, source_dir=r"C:\old\AnyPS5")
                bundle = old / "move.zip"
                counts = core.export_bundle(library, settings, bundle, include_screenshots=False, log=lambda *_: None)
                self.assertEqual((counts["games"], counts["backups"], counts["controls"]), (1, 1, 2))

            moved_source = new / "D" / "PS5" / "PPSA00001"
            moved_source.parent.mkdir(parents=True)
            source.rename(moved_source)
            with mock.patch.multiple(core, BACKUPS_DIR=new / "backups", INPUT_FILE=new / "input.ini",
                                     pictures_dir=lambda: new / "pictures"):
                library = core.Library(new / "library.json")
                settings = core.LauncherSettings(source_dir=r"C:\dev\AnyPS5")
                preview = core.read_bundle(bundle)
                self.assertEqual([item.title for item in preview["games"]], ["Moved Game"])
                found = core.find_moved_games(preview["games"], new / "D")
                self.assertEqual(found, {game.id: moved_source})
                added = core.import_bundle(bundle, library, settings, found, lambda *_: None)
                imported = added[0]
                self.assertEqual(imported.source_dir, str(moved_source))
                self.assertEqual((imported.playtime, imported.tags, imported.notes, imported.favorite), (3600, ["rpg"], "Chapter 2", True))
                self.assertIsNone(imported.converted)
                self.assertTrue(imported.restore_on_convert)
                self.assertEqual(len(core.list_backups(imported)), 1)
                self.assertEqual(core.read_input_file(core.game_input_file(imported)), {"Cross": ["KEY:J"]})
                self.assertEqual(core.read_input_file(core.INPUT_FILE), {"Circle": ["KEY:K"]})
                self.assertEqual((settings.theme, settings.fps_overlay, settings.source_dir), ("dark", True, r"C:\dev\AnyPS5"),
                                 "look and feel move; machine paths stay")
                self.assertEqual(core.import_bundle(bundle, library, settings, found, lambda *_: None), [], "no duplicates")

                staged = []
                with mock.patch.object(core, "stage_inputs", lambda g, log: staged.append(1) or g.output / "eboot.bin"), \
                        mock.patch.object(core, "mirror_app0", lambda *a, **k: None), \
                        mock.patch.object(core, "copy_libs", lambda *a, **k: None), \
                        mock.patch.object(core, "run_streaming", lambda *a, **k: (imported.output / core.OUTPUT_EXE).write_bytes(b"MZ") and 0):
                    core.convert(imported, core.Install("AnyPS5-latest", new, "x", "sha"), lambda *_: None)
                self.assertEqual((core.saves_dir(imported) / "slot.bin").read_bytes(), b"progress",
                                 "saves come back with the first conversion")
                self.assertFalse(imported.restore_on_convert)

    def test_rejects_other_zips(self):
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp) / "other.zip"
            with zipfile.ZipFile(other, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"format": "something-else"}))
            with self.assertRaisesRegex(core.LauncherError, "not an AnyPS5 launcher export"):
                core.read_bundle(other)
            unsafe = Path(temp) / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../evil.txt", "x")
            with self.assertRaisesRegex(core.LauncherError, "unsafe path"):
                core.read_bundle(unsafe)

    def test_relocate(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "New Place"
            (folder / "sce_sys").mkdir(parents=True)
            (folder / "eboot.bin").write_bytes(make_self([b"x"]))
            game = core.Game("g", "T", "", "", "old", "eboot.bin", "o", converted={"install": "x"})
            core.relocate_game(game, folder)
            self.assertEqual((game.source_dir, game.converted), (str(folder), None))


if __name__ == "__main__":
    unittest.main()
