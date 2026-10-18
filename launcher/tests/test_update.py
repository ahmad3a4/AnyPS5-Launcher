import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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



class InstallSwapTests(unittest.TestCase):
    """A swap that Windows refuses must never leave the user without an AnyPS5 install."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo, self.toolchain = root / "repo", root / "toolchain"
        (self.repo / "core" / "libs" / "prx" / "libkernel").mkdir(parents=True)
        (self.repo / "build" / "core" / "relinker").mkdir(parents=True)
        (self.repo / "build" / "core" / "relinker" / "relinker.exe").write_text("relinker")
        (self.repo / "build" / "core" / "libs" / "libs").mkdir(parents=True)
        (self.repo / "build" / "core" / "libs" / "libs" / "libkernel.prx").write_text("library")
        self.toolchain.mkdir()
        for name in core.RUNTIME_DLLS:
            (self.toolchain / name).write_text("dll")
        self.destination = root / "installs" / "AnyPS5-latest"
        self.previous = self.destination.with_name("AnyPS5-latest.old")
        (self.destination / "libs").mkdir(parents=True)
        (self.destination / "VERSION.txt").write_text("old build")
        for patcher in (mock.patch.object(core, "git_output", lambda repo, *args: "abc1234 2026-10-18"),
                        mock.patch.object(core.time, "sleep", lambda seconds: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lines = []

    def install(self, refused=0):
        """Installs while Windows refuses to rename the new build `refused` times (-1: every time)."""
        real, calls = os.replace, []

        def replace(source, target):
            if Path(source).name.endswith(".new"):
                calls.append(source)
                if refused < 0 or len(calls) <= refused:
                    raise PermissionError(13, "Access is denied")
            real(source, target)

        with mock.patch.object(core.os, "replace", replace):
            core.install_from_build(self.repo, self.toolchain, self.destination, lambda text, tag="info": self.lines.append(text))
        return calls

    def version(self):
        return (self.destination / "VERSION.txt").read_text()

    def test_install_replaces_the_previous_build(self):
        self.install()
        self.assertIn("abc1234", self.version())
        self.assertFalse(self.previous.exists())
        self.assertFalse(self.destination.with_name("AnyPS5-latest.new").exists())

    def test_a_short_lock_is_waited_out(self):
        self.assertEqual(len(self.install(refused=2)), 3)
        self.assertIn("abc1234", self.version())

    def test_a_refused_swap_keeps_the_previous_install(self):
        with self.assertRaises(core.LauncherError) as raised:
            self.install(refused=-1)
        self.assertEqual(self.version(), "old build")
        self.assertFalse(self.previous.exists())
        self.assertIn("still installed", str(raised.exception))

    def test_a_half_finished_swap_is_put_back_not_deleted(self):
        os.replace(self.destination, self.previous)
        with self.assertRaises(core.LauncherError):
            self.install(refused=-1)
        self.assertEqual(self.version(), "old build", "the earlier swap's .old folder is the install")
        self.install()
        self.assertIn("abc1234", self.version())
        self.assertFalse(self.previous.exists())


class BuildRetryTests(unittest.TestCase):
    """A build that fails once (out of memory, a busy file) is retried with fewer jobs, and the update log is kept."""

    def update(self, failures):
        commands = []

        def run(command, log, cwd=None, env=None, cancel=None):
            commands.append([str(part) for part in command])
            builds = [entry for entry in commands if entry[1] == "--build"]
            return 1 if commands[-1][1] == "--build" and len(builds) <= failures else 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, toolchain = root / "repo", root / "toolchain"
            (repo / ".git").mkdir(parents=True)
            (repo / "build").mkdir()
            (repo / "build" / "build.ninja").write_text("")
            toolchain.mkdir()
            (toolchain / "g++.exe").write_text("")
            installed = []
            with mock.patch.object(core, "run_streaming", run),                     mock.patch.object(core, "git_output", lambda repo, *args: "abc1234"),                     mock.patch.object(core, "pull_with_local_edits", lambda *args, **kwargs: None),                     mock.patch.object(core, "local_edits", lambda repo: []),                     mock.patch.object(core, "install_from_build", lambda *args: installed.append(args)):
                try:
                    core.update_from_source(repo, toolchain, root / "AnyPS5-latest", lambda text, tag="info": None)
                finally:
                    self.record = (core.DATA_DIR / core.UPDATE_LOG).read_text(encoding="utf-8")
        return commands, installed

    def test_one_failure_is_retried_with_fewer_jobs(self):
        commands, installed = self.update(failures=1)
        builds = [command for command in commands if command[1] == "--build"]
        self.assertEqual(builds[0][-1], "--parallel")
        self.assertEqual(builds[1][-2:], ["--parallel", str(max(1, (os.cpu_count() or 2) // 2))])
        self.assertEqual(len(builds), 3, "the retry, then the libs target")
        self.assertEqual(len(installed), 1)
        self.assertIn("Trying once more", self.record)
        self.assertIn("Source already at abc1234", self.record)

    def test_a_repeated_failure_names_the_saved_log(self):
        with self.assertRaises(core.LauncherError) as raised:
            self.update(failures=2)
        self.assertIn(core.UPDATE_LOG, str(raised.exception))
        self.assertIn("Trying once more", self.record)


if __name__ == "__main__":
    unittest.main()
