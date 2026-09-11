import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/delegate-workers/scripts/platform_support.py"
SPEC = importlib.util.spec_from_file_location("platform_support", SCRIPT)
platform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform)


class PlatformTests(unittest.TestCase):
    def test_project_temp_uses_git_and_worktree_roots_from_nested_directory(self):
        with temporary_directory(prefix="delegate-temp-roots-") as directory:
            for kind in ("repository", "worktree"):
                root = Path(directory).resolve() / kind
                nested = root / "src" / "中文 space"
                nested.mkdir(parents=True)
                marker = root / ".git"
                if kind == "repository":
                    marker.mkdir()
                else:
                    marker.write_text("gitdir: unused-test-marker\n", encoding="utf-8")
                target = platform.project_tmp(nested)
                self.assertEqual(target, root / "tmp")
                sentinel = target / "other-task.txt"
                sentinel.write_text("keep", encoding="utf-8")
                self.assertEqual(platform.project_tmp(nested), target)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
                self.assertFalse((nested / "tmp").exists())

    def test_project_temp_without_git_uses_invoking_directory(self):
        with temporary_directory(prefix="delegate-temp-no-git-") as directory:
            current = Path(directory).resolve()
            original_exists = Path.exists
            # Mask this test checkout's ancestor marker to simulate a non-Git project.
            with patch.object(Path, "exists", lambda path:
                              False if path.name == ".git" else original_exists(path)), \
                    patch.object(Path, "cwd", return_value=current):
                self.assertEqual(platform.project_tmp(), current / "tmp")
            self.assertTrue((current / "tmp").is_dir())

    def test_project_temp_unusable_directory_does_not_fallback(self):
        with temporary_directory(prefix="delegate-temp-blocked-") as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            (root / "tmp").write_text("user file", encoding="utf-8")
            with self.assertRaises(OSError):
                platform.project_tmp(root)
            self.assertEqual((root / "tmp").read_text(encoding="utf-8"), "user file")

    def test_project_temp_rejects_symlink_redirection(self):
        with temporary_directory(prefix="delegate-temp-links-") as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            outside = root / "other"
            outside.mkdir()
            try:
                (root / "tmp").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symbolic links unavailable")
            with self.assertRaises(OSError):
                platform.project_tmp(root)
            self.assertEqual(list(outside.iterdir()), [])

    def test_native_lock_excludes_other_process_and_releases(self):
        with temporary_directory() as directory:
            path = Path(directory) / "lock"
            code = (
                "import sys\n"
                f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
                "from platform_support import file_lock\nfrom pathlib import Path\n"
                "try:\n"
                f"    with file_lock(Path({str(path)!r})):\n        pass\n"
                "except BlockingIOError:\n    raise SystemExit(7)\n"
            )
            with platform.file_lock(path):
                result = subprocess.run([sys.executable, "-c", code], capture_output=True)
                self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(subprocess.run([sys.executable, "-c", code], capture_output=True).returncode, 0)

    def test_windows_path_entry_is_case_insensitive_and_idempotent(self):
        original = r"C:\Tools;C:\Users\Person\bin;D:\Other"
        self.assertEqual(platform.update_path(original, r"c:\users\person\BIN"), original)
        self.assertEqual(platform.update_path(original, r"c:\users\person\BIN", remove=True), r"C:\Tools;D:\Other")

    def test_windows_path_keeps_unrelated_entries_and_expansions(self):
        original = r"%USERPROFILE%\bin;;C:\Tools"
        with patch.dict(os.environ, {"USERPROFILE": r"C:\Users\Person"}):
            self.assertEqual(platform.update_path(original, r"C:\Users\Person\bin"), original)
        appended = platform.update_path(original, r"D:\Delegate Workers")
        self.assertEqual(platform.update_path(appended, r"D:\Delegate Workers", remove=True), original)

    def test_windows_python_entry_preserves_unicode_paths_and_arguments(self):
        with temporary_directory(prefix="delegate path ") as directory:
            root = Path(directory) / "中文"
            root.mkdir()
            script = root / "manager.py"
            script.write_text("import sys\nprint(repr(sys.argv))\n", encoding="utf-8")
            entry = root / "entry.py"
            entry.write_bytes(platform.windows_python_launcher(script, root / "codex home"))
            result = subprocess.run([sys.executable, "-X", "utf8", str(entry), "configure", "--profile", "custom"],
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            import ast
            self.assertEqual(ast.literal_eval(result.stdout),
                             [str(script), "--codex-home", str(root / "codex home"), "configure", "--profile", "custom"])

    @unittest.skipUnless(os.name == "nt", "Windows CMD runtime")
    def test_native_windows_batch_entry_and_exit_code(self):
        with temporary_directory(prefix="delegate command ") as directory:
            root = Path(directory) / "中文"
            root.mkdir()
            script = root / "manager.py"
            script.write_text("import sys\nprint('中文输出')\nraise SystemExit(7)\n", encoding="utf-8")
            (root / "delegate-workers-entry.py").write_bytes(platform.windows_python_launcher(script, root))
            command = root / "dw.cmd"
            command.write_bytes(platform.windows_batch_launcher())
            result = subprocess.run([str(command), "status"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 7)
            self.assertIn("中文输出", result.stdout)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true", "Ephemeral Windows CI registry")
    def test_native_user_path_round_trip(self):
        before = platform.read_user_path()
        value, value_type = before if before is not None else ("", 2)
        after = (platform.update_path(value, r"C:\delegate-workers-ci-only\中文"), value_type)
        try:
            platform.write_user_path(after)
            self.assertEqual(platform.read_user_path(), after)
        finally:
            platform.write_user_path(before)
        self.assertEqual(platform.read_user_path(), before)

    @unittest.skipUnless(os.name == "nt", "Windows CMD runtime")
    def test_native_batch_can_be_deleted_by_its_child(self):
        with temporary_directory(prefix="delegate self removal ") as directory:
            root = Path(directory)
            command = root / "dw.cmd"
            script = root / "manager.py"
            script.write_text(f"from pathlib import Path\nPath({str(command)!r}).unlink()\n", encoding="utf-8")
            (root / "delegate-workers-entry.py").write_bytes(platform.windows_python_launcher(script, root))
            command.write_bytes(platform.windows_batch_launcher())
            result = subprocess.run([str(command)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(command.exists())


if __name__ == "__main__":
    unittest.main()
