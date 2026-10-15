import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

# Never touch the real launcher data: point it at a throwaway folder before the engine is imported.
os.environ.setdefault("ANYPS5_LAUNCHER_DATA", tempfile.mkdtemp(prefix="anyps5-launcher-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anyps5core as core  # noqa: E402


def make_elf_header(phnum: int) -> bytearray:
    header = bytearray(0x40)
    header[0:4] = core.ELF_MAGIC
    header[4:7] = b"\x02\x01\x01"
    struct.pack_into("<HHIQQQIHHHHHH", header, 16, 0xFE10, 0x3E, 1, 0, 0x40, 0x9999, 0, 0x40, 0x38, phnum, 0x40, 5, 4)
    return header


def make_self(segments: list[bytes], encrypted=False, compressed=False, version=b"") -> bytes:
    phnum = len(segments) + (1 if version else 0)
    headers = b""
    for index, segment in enumerate(segments):
        offset = 0x1000 * (index + 1)
        headers += struct.pack("<IIQQQQQQ", 1, 5, offset, offset, offset, len(segment), len(segment), 0x1000)
    if version:
        offset = 0x1000 * (len(segments) + 1)
        headers += struct.pack("<IIQQQQQQ", core.PT_SCE_VERSION, 4, offset, 0, 0, len(version), len(version), 0)
    count = len(segments)
    header = struct.pack("<4sBBBBIHHQHHI", core.SELF_MAGICS[1], 0, 1, 1, 0x12, 0x101, 0, 0, 0, count, 0x22, 0)
    prefix_size = len(header) + count * 0x20 + 0x40 + len(headers)
    payloads, entries, cursor = b"", b"", (prefix_size + 0xF) & ~0xF
    for index, segment in enumerate(segments):
        payload = zlib.compress(segment) if compressed else segment
        props = core.SELF_ENTRY_BLOCKED | (index << 20) | 0x4
        props |= core.SELF_ENTRY_ENCRYPTED if encrypted else 0
        props |= core.SELF_ENTRY_COMPRESSED if compressed else 0
        entries += struct.pack("<QQQQ", props, cursor, len(payload), len(segment))
        payloads += payload
        cursor += len(payload)
    body = header + entries + bytes(make_elf_header(phnum)) + headers
    body += bytes(((prefix_size + 0xF) & ~0xF) - prefix_size)
    return body + payloads + version


class SelfTests(unittest.TestCase):
    def test_unwraps_segments_to_their_program_header_offsets(self):
        elf = core.unwrap_fake_self(make_self([b"code" * 10, b"data!"], version=b"VERSIONDATA"))
        self.assertEqual(elf[:4], core.ELF_MAGIC)
        self.assertEqual(elf[0x1000:0x1000 + 40], b"code" * 10)
        self.assertEqual(elf[0x2000:0x2005], b"data!")
        self.assertEqual(elf[0x3000:0x300B], b"VERSIONDATA")
        self.assertEqual(struct.unpack_from("<Q", elf, 0x28)[0], 0, "section headers are dropped")
        self.assertEqual(struct.unpack_from("<H", elf, 0x3C)[0], 0)

    def test_inflates_compressed_segments(self):
        segment = bytes(range(256)) * 8
        elf = core.unwrap_fake_self(make_self([segment], compressed=True))
        self.assertEqual(elf[0x1000:0x1000 + len(segment)], segment)

    def test_refuses_encrypted_self(self):
        data = make_self([b"secret"], encrypted=True)
        self.assertTrue(core.self_is_encrypted(data))
        with self.assertRaisesRegex(core.LauncherError, "encrypted"):
            core.unwrap_fake_self(data)

    def test_rejects_non_self(self):
        with self.assertRaises(core.LauncherError):
            core.unwrap_fake_self(b"\x7fELF" + bytes(100))


class GameFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.game_dir = self.root / "PPSA00001"
        (self.game_dir / "sce_sys").mkdir(parents=True)
        (self.game_dir / "sce_module").mkdir()
        (self.game_dir / "data").mkdir()
        (self.game_dir / "eboot.bin").write_bytes(make_self([b"main"]))
        (self.game_dir / "sce_module" / "libc.prx").write_bytes(b"\x7fELF" + bytes(60))
        (self.game_dir / "sce_module" / "readme.txt").write_text("not a module")
        (self.game_dir / "data" / "level.bin").write_bytes(b"level")
        (self.game_dir / "sce_sys" / "param.json").write_text(json.dumps({
            "titleId": "PPSA00001", "contentVersion": "01.000.000",
            "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Test Game"}}}))

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_reads_param_json_and_kind(self):
        info = core.inspect_game_folder(self.game_dir)
        self.assertEqual((info.title, info.title_id, info.version, info.kind), ("Test Game", "PPSA00001", "01.000.000", "self"))
        self.assertEqual(info.module_dirs, ["sce_module"])

    def test_inspect_refuses_encrypted_executable(self):
        (self.game_dir / "eboot.bin").write_bytes(make_self([b"main"], encrypted=True))
        with self.assertRaisesRegex(core.LauncherError, "decrypted dump"):
            core.inspect_game_folder(self.game_dir)

    def test_inspect_needs_an_executable(self):
        (self.game_dir / "eboot.bin").unlink()
        with self.assertRaisesRegex(core.LauncherError, "No game executable"):
            core.inspect_game_folder(self.game_dir)

    def test_suggests_extra_module_folders(self):
        plugins = self.game_dir / "Media" / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / "native.prx").write_bytes(b"\x7fELF" + bytes(60))
        self.assertEqual(core.suggest_module_dirs(self.game_dir), ["Media/Plugins"])

    def make_game(self, library: core.Library) -> core.Game:
        return library.add(core.inspect_game_folder(self.game_dir), self.root / "out", to_intel=True)

    def test_library_guards_and_persists(self):
        library = core.Library(self.root / "library.json")
        game = self.make_game(library)
        self.assertTrue(game.settings.to_intel)
        with self.assertRaisesRegex(core.LauncherError, "already in the library"):
            self.make_game(library)
        reloaded = core.Library(self.root / "library.json")
        self.assertEqual(reloaded.get(game.id).title, "Test Game")
        info = core.inspect_game_folder(self.game_dir)
        other = core.Library(self.root / "other.json")
        with self.assertRaisesRegex(core.LauncherError, "outside the game folder"):
            other.add(info, self.game_dir / "converted", to_intel=False)

    def test_stage_unwraps_and_mirror_links_without_modules(self):
        game = self.make_game(core.Library(self.root / "library.json"))
        game.output.mkdir()
        staged = core.stage_inputs(game, lambda *_: None)
        self.assertEqual(staged.read_bytes()[:4], core.ELF_MAGIC)
        self.assertTrue((game.output / core.STAGING_DIR / "sce_module" / "libc.prx").is_file())
        self.assertFalse((game.output / core.STAGING_DIR / "sce_module" / "readme.txt").exists())
        core.mirror_app0(game, lambda *_: None)
        app0 = game.output / "app0"
        self.assertEqual((app0 / "data" / "level.bin").read_bytes(), b"level")
        self.assertEqual(os.stat(app0 / "data" / "level.bin").st_nlink, 2, "game files are hard links")
        self.assertFalse((app0 / "sce_module" / "libc.prx").exists(), "converted modules are not mirrored")
        self.assertTrue((app0 / "sce_module" / "readme.txt").exists())
        core.mirror_app0(game, lambda *_: None)
        self.assertEqual((self.game_dir / "data" / "level.bin").read_bytes(), b"level", "rebuilding app0 keeps originals")

    def test_conversion_status(self):
        game = self.make_game(core.Library(self.root / "library.json"))
        install = core.Install("AnyPS5-test", self.root, "test", "abc")
        self.assertEqual(core.conversion_status(game, install)[1], "idle")
        game.output.mkdir()
        (game.output / core.OUTPUT_EXE).write_bytes(b"MZ")
        game.converted = {"install": "AnyPS5-test", "relinker_sha": "abc", "options": game.settings.relink_options()}
        self.assertEqual(core.conversion_status(game, install)[1], "ok")
        game.settings.to_intel = False
        self.assertIn("settings changed", core.conversion_status(game, install)[0])
        game.settings.to_intel = True
        self.assertIn("re-convert", core.conversion_status(game, core.Install("new", self.root, "n", "def"))[0])

    def test_install_fingerprint_follows_libraries(self):
        install = self.root / "AnyPS5-test"
        (install / "libs").mkdir(parents=True)
        (install / "relinker.exe").write_bytes(b"MZ relinker")
        (install / "libs" / "libkernel.prx").write_bytes(b"old")
        before = core.install_fingerprint(install)
        self.assertEqual(before, core.install_fingerprint(install))
        (install / "libs" / "libkernel.prx").write_bytes(b"newer")
        self.assertNotEqual(before, core.install_fingerprint(install), "a library-only update marks games for re-convert")
        self.assertEqual([item.name for item in core.find_installs(self.root)], ["AnyPS5-test"])

    def test_remove_tree_refuses_outside_owner(self):
        with self.assertRaises(core.LauncherError):
            core.remove_tree(self.game_dir, self.root / "out")
        self.assertTrue(self.game_dir.is_dir())

    def test_remove_keeps_unmarked_output(self):
        library = core.Library(self.root / "library.json")
        game = self.make_game(library)
        game.output.mkdir()
        (game.output / "precious.txt").write_text("x")
        with self.assertRaisesRegex(core.LauncherError, "not created by the launcher"):
            library.remove(game, delete_output=True)
        self.assertTrue((game.output / "precious.txt").exists())


class SettingsTests(unittest.TestCase):
    def test_input_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.ini"
            core.write_input_file({"Cross": ["KEY:F", "KEY:Space"], "R2": ["MOUSE:Right"]}, path)
            self.assertEqual(core.read_input_file(path), {"Cross": ["KEY:F", "KEY:Space"], "R2": ["MOUSE:Right"]})
            core.write_input_file({}, path)
            self.assertFalse(path.exists(), "no custom bindings means no file, so built-in bindings apply")

    def test_validate_binding(self):
        self.assertIsNone(core.validate_binding("KEY:Left Shift"))
        self.assertIsNone(core.validate_binding("MOUSE:X1"))
        self.assertIsNone(core.validate_binding("WHEEL:Down"))
        self.assertIsNotNone(core.validate_binding("MOUSE:Side"))
        self.assertIsNotNone(core.validate_binding("PAD:Cross"))
        self.assertIsNotNone(core.validate_binding("KEY:"))

    def test_game_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = core.Game("id", "T", "", "", str(root), "eboot.bin", str(root / "out"))
            game.settings = core.GameSettings(gpu="RTX", language=21, store_at_flip=True, thread_priority="high",
                                              env="FOO=bar; EMPTY=")
            input_file = root / "input.ini"
            input_file.write_text("Cross = KEY:F\n")
            with mock.patch.object(core, "INPUT_FILE", input_file), mock.patch.object(core, "FONTS_DIR", root / "fonts"):
                env = core.game_environment(game)
            self.assertEqual(env["ANYPS5_GPU"], "RTX")
            self.assertEqual(env["ANYPS5_LANGUAGE"], "21")
            self.assertEqual(env["APS5_STORE_AT_FLIP"], "1")
            self.assertEqual(env["APS5_THREAD_PRIORITY"], "high")
            self.assertEqual(env["ANYPS5_INPUT_CONFIG"], str(input_file))
            self.assertEqual(env["FOO"], "bar")
            self.assertNotIn("ANYPS5_SYSTEM_FONTS", env)

    def test_output_filter_explains_missing_import_and_folds_library_list(self):
        output = core.OutputFilter()
        lines = []
        for line in ["[time] tick", "FAIL: unresolved ELF import AbCdEfGhIjK from libkernel.prx", "Searched libraries:",
                     r"  C:\game\app0\sce_module\libc.prx.guest.prx", r"  C:\game\libs\libkernel.prx",
                     "GetLastError: 127 - The specified procedure could not be found."]:
            lines += output.feed(line)
        lines += output.flush()
        texts = [text for text, _ in lines]
        self.assertEqual(lines[1][1], "error")
        self.assertTrue(texts[2].startswith("Hint: AnyPS5 does not have the PS5 system function AbCdEfGhIjK from libkernel.prx"))
        self.assertEqual(texts[3], "Searched libraries:")
        self.assertIn("2 files", texts[4])
        self.assertTrue(texts[5].startswith("GetLastError"))
        self.assertFalse(any(text.startswith("  C:") for text in texts), "paths are folded")
        self.assertEqual(output.flush(), [])

    def test_audit_verdict_names_missing_functions(self):
        summary = {"implemented": 98, "stub": 1, "absent": 1, "module": 0, "missing_libraries": 0,
                   "absent_examples": ["AbCdEfGhIjK (libkernel.prx)"]}
        verdict, level = core.audit_verdict(summary)
        self.assertEqual(level, "error")
        self.assertIn("AbCdEfGhIjK (libkernel.prx)", verdict)
        del summary["absent_examples"]
        self.assertEqual(core.audit_verdict(summary)[1], "error", "older audits without examples still work")

    def test_audit_verdict_lets_optional_functions_through(self):
        summary = {"implemented": 99, "stub": 0, "absent": 0, "optional_missing": 1, "module": 0, "missing_libraries": 0}
        verdict, level = core.audit_verdict(summary)
        self.assertEqual(level, "ok")
        self.assertTrue(verdict.startswith("Should start"))
        self.assertIn("1 optional function it can run without is missing", verdict)
        summary["stub"] = 2
        verdict, level = core.audit_verdict(summary)
        self.assertEqual(level, "warn")
        self.assertIn("optional", verdict)
        summary["absent"] = 1
        self.assertEqual(core.audit_verdict(summary)[1], "error", "a required missing function still blocks")

    def test_hints(self):
        settings = core.GameSettings(to_intel=False)
        self.assertIn("Intel CPU fix", core.hint_for_exit(0xC000001D, settings))
        self.assertIn("Intel CPU fix", core.hint_for_exit(-1073741795, settings), "negative codes are normalised")
        self.assertIsNone(core.hint_for_exit(0, settings))
        self.assertIn("entry point not found", core.hint_for_exit(3221225785, settings))


if __name__ == "__main__":
    unittest.main()
