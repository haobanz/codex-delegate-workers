import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("manage", SCRIPTS / "manage.py")
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)
sys.path.pop(0)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="delegate-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source_skill = self.source / "skills/delegate-workers"
        shutil.copytree(ROOT / "skills/delegate-workers", self.source_skill,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.home = self.root / "codex home"
        self.home.mkdir()
        self.main_config = self.home / "config.toml"
        self.main_bytes = b'model = "chosen-by-user"\nmodel_reasoning_effort = "xhigh"\n'
        self.main_config.write_bytes(self.main_bytes)
        self.other_skill = self.home / "skills/another-skill/SKILL.md"
        self.other_skill.parent.mkdir(parents=True)
        self.other_skill.write_text("unrelated skill", encoding="utf-8")
        self.installation = manage.Installation(self.home)
        self.source_version = (self.source_skill / "VERSION").read_text().strip()

    def tearDown(self):
        self.assertEqual(self.main_config.read_bytes(), self.main_bytes)
        self.assertEqual(self.other_skill.read_text(), "unrelated skill")

    def install(self):
        return self.installation.install(self.source)

    def release(self, version="0.2.0"):
        (self.source_skill / "VERSION").write_text(version + "\n", encoding="utf-8")

    def test_install_and_reinstall_are_idempotent(self):
        self.assertEqual(self.install()["result"], "installed")
        self.assertEqual(self.install()["result"], "unchanged")
        self.assertFalse(self.installation.backups.exists())
        self.assertEqual(self.installation.status()["local_code_changes"], [])
        result = subprocess.run([str(self.installation.launcher), "status"], cwd=self.root,
                                capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["installed"])

    def test_local_install_supports_a_repository_without_commits(self):
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        self.assertEqual(self.install()["revision"], "local")

    def test_update_preserves_settings_and_unmanaged_files(self):
        self.install()
        self.installation.configure("default", "custom/model", "low", "none")
        self.installation.set_limits(parallel=2, attempts=4)
        settings = (self.installation.skill / "workers.json").read_bytes()
        extra = self.installation.skill / "personal-notes.txt"
        extra.write_text("keep this", encoding="utf-8")
        self.release()
        result = self.installation.install(self.source, require_existing=True)
        self.assertEqual(result["result"], "updated")
        self.assertEqual((self.installation.skill / "workers.json").read_bytes(), settings)
        self.assertEqual(extra.read_text(), "keep this")
        self.assertTrue(Path(result["backup"]).is_dir())

    def test_new_profile_and_default_can_be_configured(self):
        self.install()
        config = self.installation.configure("batch", "future-model", "medium", "none", True)
        self.assertEqual(config["default_profile"], "batch")
        self.assertEqual(self.installation.config()["profiles"]["batch"]["model"], "future-model")

    def test_invalid_settings_leave_previous_file_unchanged(self):
        self.install()
        previous = (self.installation.skill / "workers.json").read_bytes()
        for operation in (
                lambda: self.installation.configure("complex", fallback="default"),
                lambda: self.installation.configure("default", model="new-model"),
                lambda: self.installation.set_limits(parallel=0)):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual((self.installation.skill / "workers.json").read_bytes(), previous)

    def test_invalid_candidate_does_not_replace_installation(self):
        self.install()
        old_receipt = (self.installation.skill / manage.RECEIPT).read_bytes()
        (self.source_skill / "scripts/workers.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertEqual((self.installation.skill / manage.RECEIPT).read_bytes(), old_receipt)
        self.assertFalse(self.installation.backups.exists())

    def test_local_code_changes_block_update_without_overwriting(self):
        self.install()
        path = self.installation.skill / "SKILL.md"
        path.write_text("my edits", encoding="utf-8")
        self.release()
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertEqual(path.read_text(), "my edits")

    def test_new_upstream_file_cannot_overwrite_unmanaged_file(self):
        self.install()
        (self.installation.skill / "notes.txt").write_text("mine", encoding="utf-8")
        (self.source_skill / "notes.txt").write_text("upstream", encoding="utf-8")
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertEqual((self.installation.skill / "notes.txt").read_text(), "mine")

    def test_removed_upstream_file_is_removed_on_update(self):
        path = self.source_skill / "obsolete.txt"
        path.write_text("old", encoding="utf-8")
        self.install()
        path.unlink()
        self.install()
        self.assertFalse((self.installation.skill / "obsolete.txt").exists())

    def test_unmanaged_existing_skill_is_not_adopted(self):
        self.installation.skill.mkdir()
        path = self.installation.skill / "SKILL.md"
        path.write_text("personal", encoding="utf-8")
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertEqual(path.read_text(), "personal")

    def test_symlinked_skill_is_not_replaced(self):
        self.installation.skill.symlink_to(self.source_skill, target_is_directory=True)
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertTrue(self.installation.skill.is_symlink())

    def test_launcher_conflict_leaves_existing_file_untouched(self):
        self.installation.launcher.parent.mkdir()
        self.installation.launcher.write_text("other program", encoding="utf-8")
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertFalse(self.installation.skill.exists())
        self.assertEqual(self.installation.launcher.read_text(), "other program")

    def test_rollback_preserves_current_settings(self):
        self.install()
        self.release()
        self.install()
        self.installation.configure("default", effort="high")
        result = self.installation.rollback()
        self.assertEqual(result["version"], self.source_version)
        self.assertEqual(self.installation.config()["profiles"]["default"]["reasoning_effort"], "high")

    def test_failed_swap_restores_previous_code_and_launcher(self):
        self.install()
        old = (self.installation.skill / manage.RECEIPT).read_bytes()
        launcher = self.installation.launcher.read_bytes()
        self.release()
        rename = Path.rename

        def fail_candidate(path, target):
            if path.name.startswith(".delegate-workers-stage-"):
                raise OSError("simulated disk failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_candidate), self.assertRaises(OSError):
            self.install()
        self.assertEqual((self.installation.skill / manage.RECEIPT).read_bytes(), old)
        self.assertEqual(self.installation.launcher.read_bytes(), launcher)

    def test_uninstall_keeps_recoverable_backup(self):
        self.install()
        result = self.installation.uninstall()
        self.assertFalse(self.installation.skill.exists())
        self.assertFalse(self.installation.launcher.exists())
        self.assertFalse((self.installation.command_dir / "dw").exists())
        self.assertTrue((Path(result["backup"]) / "workers.json").is_file())
        self.installation.rollback()
        self.assertTrue(self.installation.skill.exists())

    def test_concurrent_operations_are_rejected(self):
        self.install()
        with self.installation.locked(), self.assertRaises(manage.ManagementError):
            self.installation.set_limits(parallel=2)

    def test_menu_can_configure_worker_and_limits(self):
        self.install()
        replies = io.StringIO("2\ndefault\ncustom-model\nhigh\nnone\nn\n3\n2\n4\n4\n0\n")
        with contextlib.redirect_stdout(io.StringIO()):
            manage.menu(self.installation, self.source, replies)
        config = self.installation.config()
        self.assertEqual(config["profiles"]["default"]["model"], "custom-model")
        self.assertEqual(config["max_parallel_workers"], 2)
        self.assertEqual(config["max_attempts_per_task"], 4)

    def test_menu_uses_terminal_stdin_when_dev_tty_is_unavailable(self):
        replies = io.StringIO("0\n")
        with patch("builtins.open", side_effect=OSError("no controlling terminal")), \
                patch.object(manage.sys, "stdin", replies), \
                patch.object(replies, "isatty", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            manage.menu(self.installation)

    def test_failed_download_preserves_installation(self):
        self.install()
        old = (self.installation.skill / manage.RECEIPT).read_bytes()
        with patch.object(manage, "run_git", side_effect=manage.ManagementError("offline")):
            with self.assertRaises(manage.ManagementError):
                self.installation.install()
        self.assertEqual((self.installation.skill / manage.RECEIPT).read_bytes(), old)

    def test_short_command_runs_from_path(self):
        self.install()
        environment = {**os.environ, "PATH": str(self.installation.command_dir) + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(["dw", "status"], env=environment, cwd=self.root,
                                capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["installed"])
        self.assertEqual(json.loads(result.stdout)["menu_command"], "dw")

    def test_short_command_without_arguments_opens_menu(self):
        self.install()
        replies = io.StringIO("0\n")
        with patch("builtins.open", side_effect=OSError("no controlling terminal")), \
                patch.object(manage.sys, "stdin", replies), \
                patch.object(replies, "isatty", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manage.main(["--codex-home", str(self.home)]), 0)
        self.assertIn("Worker model settings", output.getvalue())

    def test_custom_command_directory_survives_subsequent_launch(self):
        commands = self.root / "user bin"
        self.installation = manage.Installation(self.home, commands)
        self.install()
        result = subprocess.run([str(commands / "dw"), "status"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["command_dir"], str(commands))
        self.assertTrue((commands / "delegate-workers").exists())

    def test_short_name_conflict_preserves_existing_command(self):
        command = self.installation.command_dir / "dw"
        command.parent.mkdir(parents=True)
        command.write_text("another tool", encoding="utf-8")
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assertEqual(command.read_text(), "another tool")
        self.assertFalse(self.installation.skill.exists())

    def test_update_adds_short_command_to_legacy_installation(self):
        self.install()
        receipt_path = self.installation.skill / manage.RECEIPT
        receipt = json.loads(receipt_path.read_text())
        receipt.pop("command_dir")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        (self.installation.command_dir / "dw").unlink()
        self.installation = manage.Installation(self.home)
        self.install()
        self.assertTrue((self.installation.command_dir / "dw").is_file())

    def test_user_install_defaults_to_local_bin(self):
        with patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {"CODEX_HOME": ""}):
            installation = manage.Installation(self.root / ".codex")
        self.assertEqual(installation.command_dir, self.root / ".local/bin")


if __name__ == "__main__":
    unittest.main()
