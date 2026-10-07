"""CLI review boundaries, menu routing, and editor installation compatibility."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
FAKE = ROOT / "tests/fixtures/fake_codex.py"
sys.path.insert(0, str(SCRIPTS))
import manage
import project_edit
import project_rules
import worker_runtime
sys.path.pop(0)


class ProjectEditorCliTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory(prefix="project-edit-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "项目 with spaces"
        subprocess.run(["git", "init", "--quiet", str(self.project)], check=True)
        self.assertEqual(project_rules.resolve_location(self.project)["root"], self.project)
        self.home = self.root / "codex home"
        self.home.mkdir()
        self.main_bytes = b'model = "main-chosen-by-user"\nmodel_reasoning_effort = "xhigh"\n'
        (self.home / "config.toml").write_bytes(self.main_bytes)
        self.installation = manage.Installation(self.home)
        shutil.copytree(ROOT / "skills/delegate-workers", self.installation.skill,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.installation.skill / manage.RECEIPT).write_text(json.dumps({
            "project": "delegate-workers", "schema_version": 1,
            "version": "test", "revision": "local-fixture", "files": {}}), encoding="utf-8")
        self.global_worker_bytes = (self.installation.skill / "workers.json").read_bytes()
        self.instructions = self.project / "AGENTS.md"
        self.instructions.write_text("# 项目约定\n\n旧测试命令：python old.py\n", encoding="utf-8")
        self.worker = {"model": "gpt-5.6-luna", "reasoning_effort": "medium"}
        project_rules.init_project(
            self.project, template_path=self.installation.skill / "references/project-delegation.md",
            profile="default", worker=self.worker)
        self.before = self.instructions.read_bytes()
        self.state_before = (self.project / project_rules.STATE_FILE).read_bytes()

    def tearDown(self):
        self.assertEqual((self.home / "config.toml").read_bytes(), self.main_bytes)
        self.assertEqual((self.installation.skill / "workers.json").read_bytes(), self.global_worker_bytes)

    def assert_unapplied(self):
        self.assertEqual(self.instructions.read_bytes(), self.before)
        self.assertEqual((self.project / project_rules.STATE_FILE).read_bytes(), self.state_before)

    def generate_candidate(self, prepared, _home, **_kwargs):
        candidate = Path(prepared["candidate"])
        candidate.write_text(candidate.read_text(encoding="utf-8").replace("python old.py", "python new.py"),
                             encoding="utf-8")
        return {**prepared, "result": "generated"}

    def prepared_candidate(self):
        prepared = self.installation.project_prepare(self.project, "更新测试命令")
        self.generate_candidate(prepared, self.home)
        return prepared

    def cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manage.main(["--codex-home", str(self.home), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_prepare_returns_task_brief_without_calling_a_model(self):
        with patch.object(project_edit, "generate_edit") as generate:
            code, output, error = self.cli(["project", "prepare", "--path", str(self.project)])
        self.assertEqual((code, error), (0, ""))
        value = json.loads(output)
        self.assertEqual(value["result"], "prepared")
        self.assertEqual(Path(value["draft_dir"]).parents[1], self.project)
        self.assertTrue(Path(value["prompt"]).is_file())
        generate.assert_not_called()
        self.assert_unapplied()

    def test_noninteractive_missing_request_fails_before_drafting(self):
        with patch.object(project_edit, "prepare_edit") as prepare:
            code, _output, error = self.cli(["project", "edit", "--path", str(self.project)])
        self.assertEqual(code, 2)
        self.assertIn("--request", error)
        prepare.assert_not_called()
        self.assert_unapplied()

    def test_noninteractive_edit_only_generates_candidate_with_diff(self):
        with patch.object(project_edit, "generate_edit", side_effect=self.generate_candidate):
            code, output, error = self.cli([
                "project", "edit", "--path", str(self.project), "--request", "更新测试命令"])
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["result"], "draft")
        self.assertIn("+旧测试命令：python new.py", result["diff"])
        self.assertTrue(Path(result["candidate"]).is_file())
        self.assert_unapplied()

    def test_noninteractive_apply_previews_only_without_yes(self):
        prepared = self.prepared_candidate()
        code, output, error = self.cli(["project", "apply", "--draft", prepared["draft_dir"]])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["result"], "draft")
        self.assertIn("python new.py", json.loads(output)["diff"])
        self.assert_unapplied()

    def test_noninteractive_apply_yes_changes_full_file_and_reports_diff(self):
        prepared = self.prepared_candidate()
        code, output, error = self.cli(["project", "apply", "--draft", prepared["draft_dir"], "--yes"])
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["result"], "applied")
        self.assertIn("python new.py", result["diff"])
        self.assertIn("python new.py", self.instructions.read_text(encoding="utf-8"))
        self.assertTrue(Path(result["backup"]).is_dir())
        self.assertEqual(self.installation.project_status(self.project)["integrity"], "ok")

    def test_interactive_rejection_keeps_candidate_and_original_rules(self):
        prepared = self.prepared_candidate()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.installation.project_apply(prepared["draft_dir"], stream=io.StringIO("否\n"))
        self.assertEqual(result["result"], "draft")
        self.assertIn("候选差异", output.getvalue())
        self.assertTrue(Path(result["candidate"]).exists())
        self.assert_unapplied()

    def test_interactive_eof_cancels_apply(self):
        prepared = self.prepared_candidate()
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.installation.project_apply(prepared["draft_dir"], stream=io.StringIO(""))
        self.assertEqual(result["result"], "draft")
        self.assert_unapplied()

    def test_interactive_request_and_confirmation_apply_reviewed_hash(self):
        with patch.object(project_edit, "generate_edit", side_effect=self.generate_candidate), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.installation.project_edit(self.project, stream=io.StringIO("更新测试命令\n是\n"))
        self.assertEqual(result["result"], "applied")
        self.assertIn("候选差异", output.getvalue())
        self.assertIn("python new.py", self.instructions.read_text(encoding="utf-8"))

    def test_candidate_changed_during_confirmation_is_rejected(self):
        prepared = self.prepared_candidate()

        def change_after_preview(*_args, **_kwargs):
            candidate = Path(prepared["candidate"])
            candidate.write_text(candidate.read_text(encoding="utf-8") + "\n未审查的新约定\n", encoding="utf-8")
            return "是"

        with patch.object(manage, "prompt", side_effect=change_after_preview), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            self.installation.project_apply(prepared["draft_dir"], stream=io.StringIO())
        self.assert_unapplied()

    def test_generation_failure_never_applies_rules(self):
        with patch.object(project_edit, "generate_edit", side_effect=ValueError("供应商请求失败")):
            code, _output, error = self.cli([
                "project", "edit", "--path", str(self.project), "--request", "更新", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("供应商请求失败", error)
        self.assert_unapplied()

    def test_real_runtime_fake_cli_uses_project_snapshot_and_read_only(self):
        final = self.root / "fake-complete-candidate.md"
        final.write_bytes(self.before.replace(b"python old.py", b"python new.py"))
        argv_path = self.root / "fake-argv.json"
        actual_generate = project_edit.generate_edit

        def fake_provider(prepared, home):
            return actual_generate(prepared, home, command_provider=lambda: [sys.executable, str(FAKE)])

        with patch.dict(os.environ, {"FAKE_CODEX_FINAL_FILE": str(final),
                                     "FAKE_CODEX_ARGV_COPY": str(argv_path)}), \
                patch.object(project_edit, "generate_edit", side_effect=fake_provider):
            code, output, error = self.cli([
                "project", "edit", "--path", str(self.project), "--request", "更新测试命令", "--yes"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["result"], "applied")
        self.assertTrue(json.loads(output)["agent_id"].startswith("wk-"))
        self.assertEqual(json.loads(output)["requested"]["model"], self.worker["model"])
        self.assertIn("observed", json.loads(output))
        arguments = json.loads(argv_path.read_text(encoding="utf-8"))
        self.assertEqual(arguments[arguments.index("--model") + 1], self.worker["model"])
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertIn('model_reasoning_effort="medium"', arguments)
        self.assertIn("python new.py", self.instructions.read_text(encoding="utf-8"))

    def test_default_project_command_and_both_path_positions_route_to_menu(self):
        for arguments in (["project"], ["project", "--path", str(self.project)],
                          ["project", "--path", str(self.project), "menu"],
                          ["project", "menu", "--path", str(self.project)]):
            with self.subTest(arguments=arguments), patch.object(manage, "project_menu") as menu:
                code, _output, error = self.cli(arguments)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(menu.call_args.args[1], None if len(arguments) == 1 else self.project)
        self.assert_unapplied()

    def test_global_menu_eighth_entry_opens_project_menu(self):
        replies = io.StringIO(f"8\n{self.project}\n0\n")
        with patch.object(manage, "load_receipt", return_value=None), \
                patch.object(manage, "project_menu") as project_menu, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.menu(self.installation, stream=replies)
        project_menu.assert_called_once_with(self.installation, self.project, replies)
        self.assertIn("8. 项目配置与 AI 规则修改", output.getvalue())
        self.assert_unapplied()

    def test_project_menu_reconfigures_existing_snapshot_and_displays_status(self):
        config = json.loads((self.installation.skill / "workers.json").read_text(encoding="utf-8"))
        replies = io.StringIO("2\ngpt-5.6-terra\n高\n4\n0\n")
        with patch.object(self.installation, "config", return_value=config), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.project_menu(self.installation, self.project, replies)
        self.assertEqual(self.installation.project_status(self.project)["worker"],
                         {"model": "gpt-5.6-terra", "reasoning_effort": "high"})
        self.assertIn("项目执行模型：gpt-5.6-terra / high", output.getvalue())

    def test_project_menu_ai_edit_is_repeatable(self):
        replies = io.StringIO("3\n更新测试命令\n是\n3\n再审查一次\n否\n0\n")
        with patch.object(project_edit, "generate_edit", side_effect=self.generate_candidate) as generate, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.project_menu(self.installation, self.project, replies)
        self.assertEqual(generate.call_count, 2)
        self.assertIn("python new.py", self.instructions.read_text(encoding="utf-8"))
        self.assertIn("候选已保留", output.getvalue())

    def test_human_prepare_output_does_not_claim_application_or_session_loading(self):
        prepared = self.installation.project_prepare(self.project)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            manage.print_result(prepared, human=True)
        self.assertIn("AI 任务书", output.getvalue())
        self.assertIn("当前项目规则未改变", output.getvalue())
        self.assertNotIn("请重新启动", output.getvalue())
        self.assert_unapplied()

    def test_missing_optional_editor_does_not_break_project_status(self):
        with patch.dict(sys.modules, {"project_edit": None}):
            self.assertEqual(self.installation.project_status(self.project)["integrity"], "ok")
            with self.assertRaisesRegex(manage.ManagementError, "dw update"):
                self.installation.project_prepare(self.project)
        self.assert_unapplied()

    def test_human_generation_metadata_keeps_identity_unverified(self):
        prepared = self.installation.project_prepare(self.project)
        value = {**prepared, "result": "draft", "agent_id": "wk-reported-by-tool",
                 "observed": {"status": "unverified", "values": {}}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            manage.print_result(value, human=True)
        self.assertIn("wk-reported-by-tool（独立 CLI worker）", output.getvalue())
        self.assertIn("未验证", output.getvalue())
        self.assertIn("不证明上游物理模型身份", output.getvalue())


class EditorInstallationTests(unittest.TestCase):
    def test_editor_dependencies_only_apply_to_new_editor_candidates(self):
        legacy = manage.REQUIRED | manage.BASE_RUNTIME_REQUIRED
        self.assertEqual(manage.required_files_for_candidate(legacy), legacy)
        current = legacy | {"scripts/project_edit.py"}
        self.assertTrue((manage.EDITOR_RUNTIME_REQUIRED | manage.PROJECT_RUNTIME_REQUIRED
                         | manage.INTERFACE_RUNTIME_REQUIRED | manage.CAPABILITY_RUNTIME_REQUIRED)
                        <= manage.required_files_for_candidate(current))

    def test_editor_contract_is_validated_during_staging(self):
        temporary = temporary_directory(prefix="editor-install-contract-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stage = root / "stage"
        scripts = stage / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "manage.py").write_text("pass\n", encoding="utf-8")
        (scripts / "project_edit.py").write_text("# Valid syntax but missing its public API.\n", encoding="utf-8")
        with self.assertRaisesRegex(manage.ManagementError, "编辑器无法导入"):
            manage.validate_staged_candidate(stage, {"scripts/manage.py", "scripts/project_edit.py"})


if __name__ == "__main__":
    unittest.main()
