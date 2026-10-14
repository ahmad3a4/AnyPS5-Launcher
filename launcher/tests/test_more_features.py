import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Never touch the real launcher data: point it at a throwaway folder before the engine is imported.
os.environ.setdefault("ANYPS5_LAUNCHER_DATA", tempfile.mkdtemp(prefix="anyps5-launcher-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anyps5core as core  # noqa: E402
from test_anyps5core import make_self  # noqa: E402


def make_game(root: Path) -> core.Game:
    game = core.Game("g1", "Test Game", "PPSA00001", "01.000.000", str(root / "src"), "eboot.bin", str(root / "out"))
    game.output.mkdir(parents=True, exist_ok=True)
    return game


class LogTests(unittest.TestCase):
    def test_runtime_traces_are_tagged(self):
        output = core.OutputFilter()
        tags = [tag for line in ("[src/Open.cpp:139 sceKernelOpen] path=/app0/a.png flags=0x0",
                                 "[Shaders/ShaderRegistry.cpp:334 ReportPreparedAtUse] Compute shader",
                                 "[debug:0] gpu: AGC ready", "[CTrophyManager:ERROR] failed: 0x0",
                                 "FAIL: unresolved ELF import X from libkernel.prx")
                for _, tag in output.feed(line)[:1]]
        self.assertEqual(tags, ["trace", "trace", "out", "out", "error"])
        self.assertEqual(core.trace_name("[src/Thread.cpp:373 RunThread] RunThread entry=0x1"), "RunThread")
        self.assertIsNone(core.trace_name("[debug:0] hello"))

    def test_report(self):
        with tempfile.TemporaryDirectory() as temp:
            game = make_game(Path(temp))
            game.settings.gpu = "RTX"
            game.audit = {"implemented": 90, "stub": 10, "absent": 0, "module": 0, "missing_libraries": 0}
            lines = [("Starting Test Game", "info"), ("[src/Open.cpp:1 sceKernelOpen] path=/app0/x", "trace"),
                     ("FAIL: something broke", "error"), ("Hint: do this", "hint"), ("[debug:0] game text", "out")]
            report = core.build_report(game, core.Install("AnyPS5-latest", Path(temp), "AnyPS5 main abc", "x"), lines)
            self.assertIn("Game: Test Game (PPSA00001, version 01.000.000)", report)
            self.assertIn("gpu=RTX", report)
            self.assertIn("Problems in the log:\n  FAIL: something broke\n  Hint: do this", report)
            self.assertNotIn("sceKernelOpen", report, "runtime traces stay out of reports")
            self.assertIn("[debug:0] game text", report)


class ScanTests(unittest.TestCase):
    def test_scan_finds_games_and_skips_converted_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def game(folder: Path, executable: bytes, name="eboot.bin", param=True, title="Game"):
                (folder / "sce_sys").mkdir(parents=True)
                (folder / name).write_bytes(executable)
                if param:
                    (folder / "sce_sys" / "param.json").write_text(json.dumps({
                        "titleId": "PPSA0000" + title[-1], "localizedParameters": {"en-US": {"titleName": title}},
                        "defaultLanguage": "en-US"}))

            game(root / "A", make_self([b"a"]), title="Game A")
            game(root / "deep" / "er" / "B", b"\x7fELF" + bytes(60), name="eboot.elf", title="Game B")
            game(root / "C", make_self([b"c"], encrypted=True), title="Game C")
            (root / "A" / "extras").mkdir()
            converted = root / "A-anyps5"
            game(converted / "app0", make_self([b"a"]), title="Game A")
            (converted / core.MARKER_FILE).write_text("{}")
            renamed = root / "Converted copy"
            game(renamed / "app0", make_self([b"a"]), title="Game A")
            (renamed / core.MARKER_FILE).write_text("{}")
            (root / "D").mkdir()
            library = core.Library(root / "library.json")
            library.games.append(core.Game("x", "Game B", "", "", str(root / "deep" / "er" / "B"), "eboot.elf", str(root / "o")))
            results = core.scan_for_games(root, library)
            summary = [(result.folder.relative_to(root).as_posix(), bool(result.info), "encrypted" in result.error,
                        result.in_library) for result in results]
            self.assertEqual(summary, [("A", True, False, False), ("deep/er/B", True, False, True), ("C", False, True, False)])


class FontTests(unittest.TestCase):
    def test_download_fonts(self):
        with tempfile.TemporaryDirectory() as temp:
            fonts = Path(temp) / "fonts"
            fetched_urls = []

            def fake_urlopen(request, timeout=0):
                fetched_urls.append(request.full_url)
                magic = b"ttcf" if request.full_url.endswith(".ttc") else b"\x00\x01\x00\x00"
                return io.BytesIO(magic + bytes(2000))

            with mock.patch.object(core, "FONTS_DIR", fonts), mock.patch.object(core.urllib.request, "urlopen", fake_urlopen):
                self.assertEqual(core.installed_fonts(), (0, 0))
                self.assertEqual(core.download_fonts(False, lambda *_: None), len(core.FONT_FILES))
                self.assertEqual(core.installed_fonts(), (len(core.FONT_FILES), 0))
                self.assertEqual(core.download_fonts(True, lambda *_: None), 4, "only the missing CJK files are fetched")
                self.assertEqual(core.installed_fonts(), (len(core.FONT_FILES), 4))
                names = sorted(path.name for path in fonts.iterdir())
                self.assertIn("NotoSans-BoldItalic.ttf", names)
                self.assertIn("NotoSansCJK-Medium.ttc", names)
            self.assertTrue(all(url.startswith("https://raw.githubusercontent.com/notofonts/") for url in fetched_urls))

    def test_download_rejects_non_fonts(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(core, "FONTS_DIR", Path(temp)), \
                mock.patch.object(core.urllib.request, "urlopen", lambda request, timeout=0: io.BytesIO(b"<html>404</html>")):
            with self.assertRaisesRegex(core.LauncherError, "did not download as a font"):
                core.download_fonts(False, lambda *_: None)
            self.assertEqual(list(Path(temp).iterdir()), [], "a failed download leaves nothing behind")


class OrganizeTests(unittest.TestCase):
    def test_tags_and_search(self):
        self.assertEqual(core.parse_tags(" platformer, Co-op ;platformer,,  retro  2d "), ["platformer", "Co-op", "retro 2d"])
        game = core.Game("g", "Space Game", "PPSA00003", "", "s", "eboot.bin", "o", tags=["co-op"], notes="Beat level 3")
        for query in ("", "space", "ppsa00003", "CO-OP", "level 3"):
            self.assertTrue(core.matches_search(game, query), query)
        self.assertFalse(core.matches_search(game, "racing"))

    def test_per_game_controls_file_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = make_game(root)
            shared = root / "shared.ini"
            shared.write_text("Cross = KEY:F\n")
            with mock.patch.object(core, "INPUT_FILE", shared), mock.patch.object(core, "FONTS_DIR", root / "fonts"):
                self.assertEqual(core.game_environment(game)["ANYPS5_INPUT_CONFIG"], str(shared))
                core.write_input_file({"Cross": ["KEY:G"]}, core.game_input_file(game))
                self.assertTrue(core.has_own_controls(game))
                self.assertNotIn("ANYPS5_INPUT_CONFIG", core.game_environment(game),
                                 "the runtime then reads the game's own anyps5-input.ini beside eboot.exe")


class StorageTests(unittest.TestCase):
    def test_storage_counts_only_unlinked_files_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(core, "BACKUPS_DIR", root / "backups"):
                game = make_game(root)
                game.source.mkdir()
                (game.source / "big.pak").write_bytes(bytes(50_000))
                (game.output / "app0").mkdir()
                os.link(game.source / "big.pak", game.output / "app0" / "big.pak")
                (game.output / "app0" / "module.guest.prx").write_bytes(bytes(1_000))
                for folder, size in (("libs", 3_000), ("shader_cache", 7_000), (core.SAVES_DIR_NAME, 500)):
                    (game.output / folder).mkdir()
                    (game.output / folder / "file.bin").write_bytes(bytes(size))
                (game.output / "eboot.exe").write_bytes(bytes(200))
                for index in range(5):
                    with mock.patch.object(core.time, "strftime", return_value=f"20261011-1200{index:02d}"):
                        (game.output / core.SAVES_DIR_NAME / "file.bin").write_bytes(bytes([index]) * 500)
                        core.backup_saves(game, "auto")
                report = core.storage_report(game)
                self.assertEqual(report["game_files"], 1_000, "the hard-linked 50 kB file costs nothing")
                self.assertEqual((report["libraries"], report["shader_cache"], report["saves"], report["other"]),
                                 (3_000, 7_000, 500, 200))
                self.assertEqual(report["total"], 1_000 + 3_000 + 7_000 + 500 + 200 + report["backups"])
                self.assertEqual(core.clear_shader_cache(game), 7_000)
                self.assertFalse((game.output / "shader_cache").exists())
                self.assertGreater(core.prune_auto_backups(game, keep=3), 0)
                self.assertEqual(len(core.list_backups(game)), 3)
                self.assertTrue((game.source / "big.pak").is_file(), "originals are never touched")


if __name__ == "__main__":
    unittest.main()
