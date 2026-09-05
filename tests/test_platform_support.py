import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/delegate-workers/scripts/platform_support.py"
SPEC = importlib.util.spec_from_file_location("platform_support", SCRIPT)
platform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform)


class PlatformTests(unittest.TestCase):
    def test_native_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
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
        with tempfile.TemporaryDirectory(prefix="delegate path ") as directory:
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
        with tempfile.TemporaryDirectory(prefix="delegate command ") as directory:
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


if __name__ == "__main__":
    unittest.main()
