import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Never touch the real launcher data: point it at a throwaway folder before the engine is imported.
os.environ.setdefault("ANYPS5_LAUNCHER_DATA", tempfile.mkdtemp(prefix="anyps5-launcher-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anyps5core as core  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout


class PullWithLocalEditsTests(unittest.TestCase):
    """The updater must never lose edits someone made to the AnyPS5 checkout."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.upstream, self.checkout, self.other = root / "upstream.git", root / "checkout", root / "other"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.upstream)], check=True, capture_output=True, env=GIT_ENV)
        subprocess.run(["git", "clone", str(self.upstream), str(self.other)], check=True, capture_output=True, env=GIT_ENV)
        (self.other / "kernel.cpp").write_text("line 1\nline 2\nline 3\n")
        (self.other / "other.cpp").write_text("a\nb\n")
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "base")
        git(self.other, "push", "origin", "main")
        subprocess.run(["git", "clone", str(self.upstream), str(self.checkout)], check=True, capture_output=True, env=GIT_ENV)
        self.lines = []

    def tearDown(self):
        self.temp.cleanup()

    def upstream_change(self, name: str, text: str):
        (self.other / name).write_text(text)
        git(self.other, "commit", "-am", f"change {name}")
        git(self.other, "push", "origin", "main")

    def log(self, text, tag):
        self.lines.append((tag, text))

    def test_edits_that_fit_come_back(self):
        (self.checkout / "kernel.cpp").write_text("line 1\nline 2 edited locally\nline 3\n")
        self.upstream_change("other.cpp", "a\nb\nc\n")
        core.pull_with_local_edits(self.checkout, GIT_ENV, self.log)
        self.assertEqual((self.checkout / "other.cpp").read_text(), "a\nb\nc\n", "the update arrived")
        self.assertIn("edited locally", (self.checkout / "kernel.cpp").read_text(), "the edit is back")
        self.assertEqual(git(self.checkout, "stash", "list"), "", "nothing left behind in the stash")

    def test_clashing_edits_are_kept_in_the_stash(self):
        (self.checkout / "kernel.cpp").write_text("line 1\nmy version\nline 3\n")
        self.upstream_change("kernel.cpp", "line 1\nupstream version\nline 3\n")
        core.pull_with_local_edits(self.checkout, GIT_ENV, self.log)
        self.assertEqual((self.checkout / "kernel.cpp").read_text(), "line 1\nupstream version\nline 3\n",
                         "the clean new version is what gets built")
        self.assertEqual(core.local_edits(self.checkout), [])
        stash = git(self.checkout, "stash", "list")
        self.assertIn("anyps5-launcher: edits set aside", stash)
        self.assertIn("my version", git(self.checkout, "stash", "show", "-p", "stash@{0}"), "the edit is safe in the stash")
        self.assertTrue(any(tag == "warn" and "no longer fit" in text for tag, text in self.lines))

    def test_failed_pull_puts_the_edits_back(self):
        (self.checkout / "kernel.cpp").write_text("line 1\nmy version\nline 3\n")
        (self.checkout / "other.cpp").write_text("a\nb\nlocal commit\n")
        git(self.checkout, "commit", "-m", "local commit", "--", "other.cpp")
        self.upstream_change("other.cpp", "a\nb\nupstream commit\n")  # histories diverge: --ff-only refuses
        with self.assertRaisesRegex(core.LauncherError, "Your edits are back in place"):
            core.pull_with_local_edits(self.checkout, GIT_ENV, self.log)
        self.assertIn("my version", (self.checkout / "kernel.cpp").read_text())
        self.assertEqual(git(self.checkout, "stash", "list"), "")

    def test_edit_list_keeps_whole_names(self):
        (self.checkout / "kernel.cpp").write_text("changed\n")
        (self.checkout / "other.cpp").write_text("changed too\n")
        self.assertEqual(sorted(core.local_edits(self.checkout)), ["kernel.cpp", "other.cpp"])

    def test_setting_edits_aside_keeps_them(self):
        (self.checkout / "kernel.cpp").write_text("line 1\nmy version\nline 3\n")
        label = core.set_edits_aside(self.checkout, GIT_ENV, self.log)
        self.assertEqual(core.local_edits(self.checkout), [], "the official code is what remains")
        self.assertIn(label, git(self.checkout, "stash", "list"))
        self.assertIn("my version", git(self.checkout, "stash", "show", "-p", "stash@{0}"))
        self.assertIsNone(core.set_edits_aside(self.checkout, GIT_ENV, self.log), "nothing to set aside")

    def test_clean_checkout_just_pulls(self):
        self.upstream_change("other.cpp", "new\n")
        core.pull_with_local_edits(self.checkout, GIT_ENV, self.log)
        self.assertEqual((self.checkout / "other.cpp").read_text(), "new\n")
        self.assertFalse(any("Setting them aside" in text for _, text in self.lines))


if __name__ == "__main__":
    unittest.main()
