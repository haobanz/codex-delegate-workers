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

    def release(self, version="9.9.9"):
        (self.source_skill / "VERSION").write_text(version + "\n", encoding="utf-8")

    def installation_snapshot(self):
        paths = [
            self.installation.skill / manage.RECEIPT,
            self.installation.skill / "workers.json",
            self.home / "AGENTS.md",
            self.home / "AGENTS.override.md",
            *self.installation.launchers(),
        ]
        return {path: path.read_bytes() if path.exists() else None for path in set(paths)}

    def assert_installation_snapshot(self, snapshot):
        for path, contents in snapshot.items():
            self.assertEqual(path.read_bytes() if path.exists() else None, contents, path)

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
        self.installation.configure("default", "custom/model", "low")
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
        config = self.installation.configure("batch", "future-model", "medium", default=True)
        self.assertEqual(config["default_profile"], "batch")
        self.assertEqual(self.installation.config()["profiles"]["batch"]["model"], "future-model")

    def test_invalid_settings_leave_previous_file_unchanged(self):
        self.install()
        previous = (self.installation.skill / "workers.json").read_bytes()
        for operation in (
                lambda: self.installation.configure("complex", effort="unknown"),
                lambda: self.installation.configure("default", model="new-model"),
                lambda: self.installation.configure("default", model="", effort="medium")):
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

    def test_missing_runtime_module_rejects_update_without_state_changes(self):
        self.install()
        self.installation.configure("default", "custom/model", "low")
        before = self.installation_snapshot()
        (self.source_skill / "scripts/activation.py").unlink()
        self.release()
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assert_installation_snapshot(before)
        self.assertEqual(self.installation.status()["activation"]["mode"], "auto")
        self.assertFalse(self.installation.backups.exists())

    def test_missing_template_rejects_update_without_disabling_auto_mode(self):
        self.install()
        before = self.installation_snapshot()
        (self.source_skill / "references/default-delegation.md").unlink()
        self.release()
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assert_installation_snapshot(before)
        self.assertEqual(self.installation.status()["activation"]["mode"], "auto")
        self.assertTrue(self.installation.status()["activation"]["rule_present"])

    def test_management_syntax_failure_rejects_update_without_state_changes(self):
        self.install()
        before = self.installation_snapshot()
        (self.source_skill / "scripts/manage.py").write_text("def broken(:\n", encoding="utf-8")
        self.release()
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assert_installation_snapshot(before)

    def test_management_import_failure_rejects_update_without_state_changes(self):
        self.install()
        before = self.installation_snapshot()
        candidate = self.source_skill / "scripts/manage.py"
        candidate.write_text("import module_that_does_not_exist\n" + candidate.read_text(),
                             encoding="utf-8")
        self.release()
        with self.assertRaises(manage.ManagementError):
            self.install()
        self.assert_installation_snapshot(before)

    def test_rollback_rejects_backup_missing_runtime_dependency_without_state_changes(self):
        self.install()
        self.release()
        result = self.install()
        backup = Path(result["backup"])
        (backup / "scripts/activation.py").unlink()
        before = self.installation_snapshot()
        with self.assertRaises(manage.ManagementError):
            self.installation.rollback()
        self.assert_installation_snapshot(before)
        self.assertEqual(self.installation.status()["activation"]["mode"], "auto")

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
            self.installation.configure("default", effort="high")

    def test_menu_can_configure_worker(self):
        self.install()
        replies = io.StringIO("2\ndefault\ncustom-model\nhigh\nn\n3\n0\n")
        with contextlib.redirect_stdout(io.StringIO()):
            manage.menu(self.installation, self.source, replies)
        config = self.installation.config()
        self.assertEqual(config["profiles"]["default"]["model"], "custom-model")
        self.assertEqual(config["profiles"]["default"]["reasoning_effort"], "high")

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
        self.assertIn("设置执行模型和思考强度", output.getvalue())

    def test_menu_accepts_chinese_and_numeric_effort_choices(self):
        self.install()
        for choice in ("高", "3"):
            with self.subTest(choice=choice):
                replies = io.StringIO(f"2\ncustom\ncustom-model\n{choice}\n是\n0\n")
                with contextlib.redirect_stdout(io.StringIO()):
                    manage.menu(self.installation, self.source, replies)
                config = self.installation.config()
                self.assertEqual(config["default_profile"], "custom")
                self.assertEqual(config["profiles"]["custom"]["reasoning_effort"], "high")
                self.assertEqual(set(config["profiles"]["custom"]), {"model", "reasoning_effort"})

    def test_menu_accepts_chinese_uninstall_confirmation(self):
        self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            manage.menu(self.installation, self.source, io.StringIO("6\n卸载\n"))
        self.assertFalse(self.installation.skill.exists())
        self.assertTrue(any(self.installation.backups.iterdir()))

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

    def test_install_enables_default_delegation(self):
        self.install()
        state = self.installation.status()["activation"]
        self.assertEqual(state["mode"], "auto")
        self.assertTrue(state["rule_present"])
        self.assertIn(str(self.installation.skill / "SKILL.md").encode(),
                      (self.home / "AGENTS.md").read_bytes())

    def test_turn_off_restores_exact_existing_instructions(self):
        path = self.home / "AGENTS.md"
        original = b"# Personal rules\r\nPreserve these bytes without a trailing newline."
        path.write_bytes(original)
        self.install()
        self.installation.set_mode("on-demand")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.installation.status()["activation"]["mode"], "on-demand")
        self.release()
        self.install()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.installation.status()["activation"]["mode"], "on-demand")

    def test_uninstall_then_restore_preserves_disabled_mode(self):
        self.install()
        self.installation.set_mode("on-demand")
        self.installation.uninstall()
        self.installation.rollback()
        self.assertEqual(self.installation.status()["activation"]["mode"], "on-demand")
        self.assertFalse((self.home / "AGENTS.md").exists())

    def test_disable_removes_only_created_file_and_keeps_later_user_edits(self):
        self.install()
        path = self.home / "AGENTS.md"
        self.installation.set_mode("on-demand")
        self.assertFalse(path.exists())
        self.installation.set_mode("auto")
        with path.open("ab") as stream:
            stream.write(b"# Added after installation\n")
        self.installation.uninstall()
        self.assertEqual(path.read_bytes(), b"# Added after installation\n")

    def test_existing_override_is_used_and_restored(self):
        base = self.home / "AGENTS.md"
        override = self.home / "AGENTS.override.md"
        base.write_bytes(b"# Base rules\n")
        override.write_bytes(b"# Active override\n")
        self.install()
        self.assertEqual(self.installation.status()["activation"]["file"], str(override))
        self.assertEqual(base.read_bytes(), b"# Base rules\n")
        self.installation.uninstall()
        self.assertEqual(override.read_bytes(), b"# Active override\n")

    def test_later_override_is_reported_and_rule_can_migrate(self):
        self.install()
        override = self.home / "AGENTS.override.md"
        override.write_bytes(b"# New override\n")
        state = self.installation.status()["activation"]
        self.assertFalse(state["rule_present"])
        self.assertIsNotNone(state["issue"])
        self.installation.set_mode("auto")
        self.assertTrue(self.installation.status()["activation"]["rule_present"])
        self.assertFalse((self.home / "AGENTS.md").exists())
        self.installation.set_mode("on-demand")
        self.assertEqual(override.read_bytes(), b"# New override\n")

    def test_changed_owned_rule_is_not_overwritten(self):
        self.install()
        path = self.home / "AGENTS.md"
        path.write_bytes(path.read_bytes().replace(b"Default Worker Delegation", b"User changed this block"))
        original = path.read_bytes()
        self.assertFalse(self.installation.status()["activation"]["rule_present"])
        with self.assertRaises(ValueError):
            self.install()
        with self.assertRaises(ValueError):
            self.installation.set_mode("auto")
        self.assertEqual(path.read_bytes(), original)

    def test_deleted_rule_is_detected_and_explicitly_repaired(self):
        self.install()
        path = self.home / "AGENTS.md"
        path.unlink()
        self.assertFalse(self.installation.status()["activation"]["rule_present"])
        with self.assertRaises(ValueError):
            self.install()
        self.installation.set_mode("auto")
        self.assertTrue(self.installation.status()["activation"]["rule_present"])

    def test_failed_mode_save_restores_instruction_and_receipt(self):
        self.install()
        before = (self.home / "AGENTS.md").read_bytes()
        receipt = (self.installation.skill / manage.RECEIPT).read_bytes()
        original_write = manage.atomic_write

        def fail_receipt(path, data, *args):
            if path.name == manage.RECEIPT:
                raise OSError("simulated receipt write failure")
            return original_write(path, data, *args)

        with patch.object(manage, "atomic_write", fail_receipt), self.assertRaises(OSError):
            self.installation.set_mode("on-demand")
        self.assertEqual((self.home / "AGENTS.md").read_bytes(), before)
        self.assertEqual((self.installation.skill / manage.RECEIPT).read_bytes(), receipt)

    def test_failed_update_restores_old_rule(self):
        self.install()
        path = self.home / "AGENTS.md"
        original = path.read_bytes()
        template = self.source_skill / "references/default-delegation.md"
        template.write_text(template.read_text() + "\nNew rule revision.\n", encoding="utf-8")
        rename = Path.rename

        def fail_candidate(path, target):
            if path.name.startswith(".delegate-workers-stage-"):
                raise OSError("simulated swap failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_candidate), self.assertRaises(OSError):
            self.install()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.installation.status()["activation"]["rule_present"])

    def test_mode_can_be_changed_from_menu(self):
        self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            manage.menu(self.installation, self.source, io.StringIO("4\n2\n0\n"))
        self.assertEqual(self.installation.status()["activation"]["mode"], "on-demand")

    def test_legacy_settings_migrate_without_changing_models_or_activation(self):
        self.install()
        self.installation.set_mode("on-demand")
        legacy = {"version": 1, "default_profile": "default", "max_parallel_workers": 3,
                  "max_attempts_per_task": 2, "profiles": {
                      "default": {"model": "gpt-5.6-luna", "reasoning_effort": "max", "fallback": "complex"},
                      "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "xhigh", "fallback": None}}}
        config_path = self.installation.skill / "workers.json"
        config_path.write_text(json.dumps(legacy), encoding="utf-8")
        original = config_path.read_bytes()
        self.install()
        current = json.loads(config_path.read_text())
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["profiles"], {
            "default": {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"}})
        self.assertEqual((self.installation.skill / "workers.json.bak").read_bytes(), original)
        self.assertEqual(set(current), {"version", "default_profile", "profiles"})
        self.assertEqual(self.installation.status()["activation"]["mode"], "on-demand")

    def test_unmanaged_python_file_is_preserved_without_becoming_release_dependency(self):
        self.install()
        extra = self.installation.skill / "unfinished-personal-script.py"
        extra.write_text("def unfinished(:\n", encoding="utf-8")
        self.release()
        self.install()
        self.assertEqual(extra.read_text(), "def unfinished(:\n")

    def test_unknown_rule_block_is_not_adopted(self):
        path = self.home / "AGENTS.md"
        original = b"\n<!-- delegate-workers:begin -->\nUnknown rule\n<!-- delegate-workers:end -->\n"
        path.write_bytes(original)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.installation.skill.exists())


if __name__ == "__main__":
    unittest.main()
