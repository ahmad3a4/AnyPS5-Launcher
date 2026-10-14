import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

# Never touch the real launcher data: point it at a throwaway folder before the engine is imported.
os.environ.setdefault("ANYPS5_LAUNCHER_DATA", tempfile.mkdtemp(prefix="anyps5-launcher-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anyps5core as core  # noqa: E402


def make_game(root: Path, **fields) -> core.Game:
    game = core.Game("g1", "Test Game", "PPSA00001", "01.000.000", str(root / "src"), "eboot.bin", str(root / "out"), **fields)
    game.output.mkdir(parents=True, exist_ok=True)
    return game


class HistoryAndPlaytimeTests(unittest.TestCase):
    def summary(self, absent, at, implemented=90):
        return {"implemented": implemented, "stub": 5, "absent": absent, "module": 0, "missing_libraries": 0, "at": at}

    def test_history_records_changes_and_folds_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            game.converted = {"install": "AnyPS5-latest", "relinker_sha": "aaa"}
            core.record_audit_history(game, self.summary(59, "2026-10-11 10:00"))
            core.record_audit_history(game, self.summary(59, "2026-10-12 10:00"))
            self.assertEqual(len(game.audit_history), 1, "same build and counts only refresh the date")
            self.assertEqual(game.audit_history[0]["at"], "2026-10-12 10:00")
            self.assertIsNone(core.history_trend(game))
            game.converted["relinker_sha"] = "bbb"
            core.record_audit_history(game, self.summary(41, "2026-10-20 10:00", implemented=108))
            self.assertEqual(len(game.audit_history), 2)
            self.assertEqual(core.history_trend(game), "Missing functions down from 59 to 41 since 2026-10-12")
            self.assertAlmostEqual(core.implemented_share(game.audit_history[-1]), 100 * 108 / 154)

    def test_history_is_capped(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            for index in range(core.HISTORY_KEPT + 5):
                game.converted = {"relinker_sha": str(index)}
                core.record_audit_history(game, self.summary(index, "2026-10-11 10:00"))
            self.assertEqual(len(game.audit_history), core.HISTORY_KEPT)
            self.assertEqual(game.audit_history[-1]["absent"], core.HISTORY_KEPT + 4)

    def test_playtime(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            core.record_session(game, 1500)
            core.record_session(game, 4000)
            self.assertEqual((game.playtime, game.sessions, game.last_session), (5500, 2, 4000))
            self.assertIsNotNone(game.last_played)
        self.assertEqual(core.format_duration(0), "0 min")
        self.assertEqual(core.format_duration(30), "under a minute")
        self.assertEqual(core.format_duration(25 * 60), "25 min")
        self.assertEqual(core.format_duration(3 * 3600 + 12 * 60), "3 h 12 min")
        self.assertEqual(core.format_duration(7200), "2 h")

    def test_old_library_entries_load_with_defaults(self):
        game = core.Game.from_dict({"id": "x", "title": "T", "title_id": "", "version": "", "source_dir": "s",
                                    "executable": "eboot.bin", "output_dir": "o", "settings": {"gpu": "RTX"}})
        self.assertEqual((game.playtime, game.sessions, game.audit_history), (0, 0, []))
        self.assertEqual(game.settings.gpu, "RTX")

    def test_find_game_by_id_or_title_id(self):
        with tempfile.TemporaryDirectory() as temp:
            library = core.Library(Path(temp) / "library.json")
            game = make_game(Path(temp))
            library.games.append(game)
            self.assertIs(core.find_game(library, "g1"), game)
            self.assertIs(core.find_game(library, "ppsa00001"), game)
            self.assertIsNone(core.find_game(library, "PPSA99999"))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(core, "BACKUPS_DIR", self.root / "backups")
        self.patch.start()
        self.game = make_game(self.root)
        self.saves = core.saves_dir(self.game)
        (self.saves / "slot1").mkdir(parents=True)
        (self.saves / "slot1" / "data.bin").write_bytes(b"level 3")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_backup_and_restore_round_trip(self):
        backup = core.backup_saves(self.game)
        self.assertEqual(backup.kind, "manual")
        (self.saves / "slot1" / "data.bin").write_bytes(b"level 9")
        (self.saves / "slot2").mkdir()
        (self.saves / "slot2" / "new.bin").write_bytes(b"x")
        safety = core.restore_backup(self.game, backup.path)
        self.assertEqual(safety.kind, "before-restore", "current saves are kept before restoring")
        self.assertEqual((self.saves / "slot1" / "data.bin").read_bytes(), b"level 3")
        self.assertFalse((self.saves / "slot2").exists())
        core.restore_backup(self.game, safety.path)
        self.assertEqual((self.saves / "slot1" / "data.bin").read_bytes(), b"level 9")

    def test_auto_backup_skips_unchanged_and_prunes(self):
        self.assertIsNotNone(core.backup_saves(self.game, "auto"))
        self.assertIsNone(core.backup_saves(self.game, "auto"), "unchanged saves are not backed up again")
        with mock.patch.object(core, "AUTO_BACKUPS_KEPT", 2), mock.patch.object(core.time, "sleep"):
            for index in range(4):
                (self.saves / "slot1" / "data.bin").write_bytes(f"v{index}".encode())
                stamp = f"20261011-1200{index:02d}"
                with mock.patch.object(core.time, "strftime", return_value=stamp):
                    core.backup_saves(self.game, "auto")
        self.assertEqual(len([b for b in core.list_backups(self.game) if b.kind == "auto"]), 2)

    def test_no_saves_means_no_backup(self):
        core.remove_tree(self.saves, self.game.output)
        self.assertIsNone(core.backup_saves(self.game))

    def test_import_checks_owner_and_safety(self):
        exported = self.root / "exported.zip"
        with zipfile.ZipFile(exported, "w") as archive:
            archive.writestr(core.BACKUP_MANIFEST, json.dumps({"title": "Other", "title_id": "PPSA99999"}))
            archive.writestr("_sd/a.bin", b"a")
        with self.assertRaisesRegex(core.LauncherError, "belongs to Other"):
            core.import_backup(self.game, exported)
        unsafe = self.root / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("_sd/../../evil.txt", b"x")
        with self.assertRaisesRegex(core.LauncherError, "unsafe path"):
            core.import_backup(self.game, unsafe)
        good = core.backup_saves(self.game)
        imported = core.import_backup(self.game, good.path)
        self.assertEqual(imported.kind, "imported")

    def test_remove_with_output_keeps_a_backup(self):
        library = core.Library(self.root / "library.json")
        library.games.append(self.game)
        (self.game.output / core.MARKER_FILE).write_text("{}")
        library.remove(self.game, delete_output=True)
        self.assertFalse(self.game.output.exists())
        self.assertEqual([b.kind for b in core.list_backups(self.game)], ["before-remove"])


class PerformanceTests(unittest.TestCase):
    def test_parse_fps_title(self):
        self.assertEqual(core.parse_fps_title("DOOM | FPS: 59.94 (1234)"), (59.94, 1234))
        self.assertEqual(core.parse_fps_title("A | B | FPS: 35.00 (7)"), (35.0, 7))
        self.assertIsNone(core.parse_fps_title("DOOM"))
        self.assertIsNone(core.parse_fps_title(""))

    def test_tracker_skips_warmup_and_repeats(self):
        tracker = core.PerformanceTracker(warmup=10)
        tracker.add(0, 10.0, 10)            # loading: ignored
        tracker.add(5, 20.0, 100)           # still warming up
        self.assertIsNone(tracker.summary())
        tracker.add(10, 55.0, 400)
        tracker.add(11, 55.0, 400)          # title not updated since: ignored
        tracker.add(12, 60.0, 520)
        tracker.add(14, 40.0, 600)
        tracker.add(20, 60.0, 960)
        summary = tracker.summary()
        self.assertEqual(summary["avg"], 53.3, "mean of the readings after the first: (60 + 40 + 60) / 3")
        self.assertEqual((summary["low"], summary["high"], summary["seconds"]), (40.0, 60.0, 10))
        self.assertTrue(summary["low"] <= summary["avg"] <= summary["high"])

    def test_performance_history(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            self.assertIsNone(core.performance_text(game))
            for index in range(core.PERFORMANCE_KEPT + 2):
                core.record_performance(game, {"avg": 60.0, "low": 50.0, "high": 61.0, "seconds": 120})
            self.assertEqual(len(game.performance), core.PERFORMANCE_KEPT)
            self.assertIn("60.0 FPS average, lowest 50.0", core.performance_text(game))

    @unittest.skipUnless(os.name == "nt", "Windows windows")
    def test_window_helpers_tolerate_missing_windows(self):
        self.assertEqual(core.find_window(0x7FFFFFF0), 0)
        self.assertIsInstance(core.foreground_window(), int)

    @unittest.skipUnless(os.name == "nt", "XInput")
    def test_xinput_reads_without_controllers(self):
        pad = core.XInput()
        self.assertTrue(pad.available)
        for index in pad.connected():
            state = pad.read(index)
            self.assertTrue(-1 <= state["lx"] <= 1 and 0 <= state["r2"] <= 1)
        self.assertIsNone(pad.read(3) if 3 not in pad.connected() else None)


class ShortcutFolderTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_shortcut_folder_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = core.Library(root / "library.json")
            first = make_game(root)
            second = core.Game("g2", "Test Game", "PPSA00002", "", str(root / "src2"), "eboot.bin", str(root / "out2"))
            library.games = [first, second]
            folder = root / "AnyPS5 Games"
            folder.mkdir()
            (folder / "Removed Game.lnk").write_bytes(b"stale")
            written, removed = core.sync_shortcut_folder(library.games, folder)
            names = sorted(path.name for path in folder.glob("*.lnk"))
            self.assertEqual((written, removed), (3, 1))
            self.assertEqual(names, ["AnyPS5 Launcher.lnk", "Test Game (PPSA00002).lnk", "Test Game.lnk"],
                             "same titles get their title ID")
            core.remove_shortcut_folder(folder)
            self.assertFalse(folder.exists())

    def test_safe_filename(self):
        self.assertEqual(core.safe_filename('Game: "Deluxe" <Edition>?'), "Game Deluxe Edition")
        self.assertEqual(core.safe_filename("..."), "Game")

    @unittest.skipUnless(os.name == "nt", "Windows shortcuts")
    def test_game_shortcut(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            game.title = 'It\'s "Quoted" $env:PATH'
            link = core.create_game_shortcut(game, Path(temp))
            self.assertTrue(link.is_file())
            self.assertEqual(link.name, "It's Quoted $envPATH.lnk")

    def test_single_instance_forwarding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "instance.json"
            self.assertFalse(core.SingleInstance(path).forward({"command": "show"}), "no running launcher")
            received, arrived = [], threading.Event()
            primary = core.SingleInstance(path)
            primary.serve(lambda message: (received.append(message), arrived.set()))
            try:
                self.assertTrue(core.SingleInstance(path).forward({"command": "play", "game": "g1"}))
                self.assertTrue(arrived.wait(5))
                self.assertEqual(received, [{"command": "play", "game": "g1"}])
                info = json.loads(path.read_text())
                info["token"] = "wrong"
                path.write_text(json.dumps(info))
                self.assertFalse(core.SingleInstance(path).forward({"command": "play"}), "a wrong token is refused")
            finally:
                primary.close()
            self.assertFalse(core.SingleInstance(path).forward({"command": "show"}))


if __name__ == "__main__":
    unittest.main()
