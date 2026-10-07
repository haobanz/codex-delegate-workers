import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
sys.path.insert(0, str(SCRIPTS))
import project_edit
import project_rules
import worker_runtime

TEMPLATE = ROOT / "skills/delegate-workers/references/project-delegation.md"
WORKER = {"model": "gpt-5.6-luna", "reasoning_effort": "medium"}
OTHER = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}


class ProjectEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory(prefix="delegate-project-edit-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.assertEqual(project_rules.resolve_project_root(self.root, mutation=True), self.root)
        (self.root / "AGENTS.md").write_bytes(b"# Project\n\nRun pytest before delivery.\n")
        project_rules.init_project(self.root, template_path=TEMPLATE, profile="default", worker=WORKER)

    def snapshot(self):
        return {name: project_rules._read_file(self.root / name)
                for name in project_edit.BASELINE_FILES}

    def draft(self):
        return project_edit.prepare_edit(self.root, request="Use pytest -q and require evidence.")

    def candidate(self, prepared, transform=None):
        path = Path(prepared["candidate"])
        raw = path.read_bytes()
        if transform is None:
            raw = raw.replace(b"Run pytest before delivery.", b"Run pytest -q before delivery.")
            raw = raw.replace(b"## Project Worker Delegation",
                              b"## Project Worker Delegation\n\nWorkers must report uncertain requirements with evidence.")
        else:
            raw = transform(raw)
        path.write_bytes(raw)
        return raw

    def apply(self, prepared):
        view = project_edit.preview_edit(prepared["draft_dir"])
        return project_edit.apply_edit(prepared["draft_dir"], candidate_sha256=view["candidate_sha256"])

    def state(self):
        return json.loads((self.root / project_rules.STATE_FILE).read_text(encoding="utf-8"))

    def require_symlinks(self):
        target = self.directory / "symlink-target"
        link = self.directory / "symlink-link"
        target.write_bytes(b"x")
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        finally:
            link.unlink(missing_ok=True)

    def test_prepare_is_local_readonly_and_has_complete_task_brief(self):
        before = self.snapshot()
        with patch.object(worker_runtime.Runtime, "spawn", side_effect=AssertionError("no model request")):
            prepared = self.draft()
            view = project_edit.preview_edit(prepared["draft_dir"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(view["diff"], "")
        self.assertEqual(prepared["worker"], WORKER)
        draft = Path(prepared["draft_dir"])
        draft.relative_to(self.root / "tmp")
        self.assertIn("Run pytest before delivery.", Path(prepared["prompt"]).read_text(encoding="utf-8"))
        self.assertIn("不修改任何项目文件", Path(prepared["prompt"]).read_text(encoding="utf-8"))

    def test_apply_replaces_full_document_and_owned_sha_in_one_transaction(self):
        before = self.snapshot()
        os.chmod(self.root / "AGENTS.md", 0o640)
        expected_mode = (self.root / "AGENTS.md").stat().st_mode & 0o777
        prepared = self.draft()
        candidate = self.candidate(prepared)
        view = project_edit.preview_edit(prepared["draft_dir"])
        self.assertIn("pytest -q", view["diff"])
        result = self.apply(prepared)
        self.assertEqual(result["result"], "applied")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), candidate)
        state = self.state()
        self.assertEqual(state["sha256"], project_rules._marker_info(candidate)["sha256"])
        self.assertIn("Workers must report uncertain requirements", state["custom_rule"])
        self.assertEqual((self.root / "AGENTS.md").stat().st_mode & 0o777, expected_mode)
        self.assertEqual((Path(result["backup"]) / "AGENTS.md").read_bytes(), before["AGENTS.md"])
        self.assertEqual(project_rules.status_project(self.root)["integrity"], "ok")

    def test_apply_is_repeatable_without_rewrite_or_additional_backup(self):
        prepared = self.draft()
        self.candidate(prepared)
        first = self.apply(prepared)
        before = self.snapshot()
        mtimes = {name: (self.root / name).stat().st_mtime_ns for name in before if before[name] is not None}
        backups = sorted((self.root / project_rules.BACKUP_DIR).iterdir())
        second = self.apply(prepared)
        self.assertEqual(second["result"], "unchanged")
        self.assertIsNone(second["backup"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(backups, sorted((self.root / project_rules.BACKUP_DIR).iterdir()))
        self.assertEqual(mtimes, {name: (self.root / name).stat().st_mtime_ns for name in mtimes})
        self.assertIsNotNone(first["backup"])

    def test_custom_snapshot_survives_sync_reconfiguration_disable_reenable(self):
        prepared = self.draft()
        self.candidate(prepared, lambda raw: raw.replace(
            b"## Project Worker Delegation", b"## Project Worker Delegation\n\nAI custom policy: use `"
            + WORKER["model"].encode() + b"` / `medium`; bare medium is prose."))
        self.apply(prepared)
        custom = self.state()["custom_rule"]
        self.assertNotIn(WORKER["model"], custom)
        self.assertIn("bare medium is prose", custom)
        self.assertEqual(project_rules.sync_project(self.root, template_path=TEMPLATE)["result"], "unchanged")
        project_rules.init_project(self.root, template_path=TEMPLATE, profile="complex", worker=OTHER)
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("AI custom policy: use `gpt-5.6-terra` / `high`", agents)
        self.assertNotIn(WORKER["model"], agents.split(project_rules.BEGIN_TOKEN.decode())[1])
        self.assertIn("bare medium is prose", agents)
        self.assertEqual(custom, self.state()["custom_rule"])
        project_rules.disable_project(self.root)
        self.assertEqual(custom, self.state()["custom_rule"])
        project_rules.init_project(self.root, template_path=TEMPLATE)
        self.assertIn("AI custom policy", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual(custom, self.state()["custom_rule"])

    def test_custom_body_survives_override_migration(self):
        prepared = self.draft()
        self.candidate(prepared)
        self.apply(prepared)
        custom = self.state()["custom_rule"]
        (self.root / "AGENTS.override.md").write_text("# Override\n", encoding="utf-8")
        project_rules.sync_project(self.root, template_path=TEMPLATE)
        self.assertEqual(self.state()["file"], "AGENTS.override.md")
        self.assertEqual(self.state()["custom_rule"], custom)
        self.assertIn("uncertain requirements", (self.root / "AGENTS.override.md").read_text(encoding="utf-8"))

    def test_short_or_effort_named_model_ids_keep_canonical_template_fields(self):
        for worker in ({"model": "model", "reasoning_effort": "high"},
                       {"model": "max", "reasoning_effort": "max"},
                       {"model": "reasoning_effort", "reasoning_effort": "high"}):
            with self.subTest(worker=worker):
                body = ("## Delegation\n\n- model: `{model}`\n"
                        "- reasoning effort: `{reasoning_effort}`\n\n"
                        "Use `{model}` and `{reasoning_effort}` in the model routing prose.\n")
                block = project_rules._render_block(body.encode(), worker, b"\n")
                candidate = b"# Project\n" + block
                actual, info, custom = project_edit._candidate_details(candidate, worker, b"\n")
                self.assertEqual(actual, candidate)
                self.assertIn("- model: `{model}`", custom)
                self.assertIn("- reasoning effort: `{reasoning_effort}`", custom)
                project_rules._validate_custom_rule(custom)
                self.assertEqual(project_rules._render_block(custom.encode(), worker, b"\n"),
                                 info["block"])
                rebound = project_rules._render_block(custom.encode(), OTHER, b"\n")
                self.assertIn(b"- model: `gpt-5.6-terra`", rebound)
                self.assertIn(b"- reasoning effort: `high`", rebound)

    def test_explicit_prepare_can_readopt_valid_manual_change(self):
        path = self.root / "AGENTS.md"
        path.write_bytes(path.read_bytes().replace(b"## Project Worker Delegation", b"## Human revised delegation"))
        with self.assertRaises(project_rules.ProjectError):
            project_rules.sync_project(self.root, template_path=TEMPLATE)
        prepared = self.draft()
        self.apply(prepared)
        self.assertEqual(project_rules.status_project(self.root)["integrity"], "ok")
        self.assertIn("Human revised delegation", self.state()["custom_rule"])

    def test_both_instruction_files_and_state_changes_refuse_apply(self):
        for name in project_edit.BASELINE_FILES:
            with self.subTest(name=name):
                prepared = self.draft()
                self.candidate(prepared)
                view = project_edit.preview_edit(prepared["draft_dir"])
                target = self.root / name
                original = project_rules._read_file(target)
                target.write_bytes((original or b"") + b"\nconcurrent user edit\n")
                before = self.snapshot()
                with self.assertRaises(project_rules.ProjectError):
                    project_edit.apply_edit(prepared["draft_dir"], candidate_sha256=view["candidate_sha256"])
                self.assertEqual(before, self.snapshot())
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_candidate_change_after_review_refuses_apply(self):
        prepared = self.draft()
        self.candidate(prepared)
        view = project_edit.preview_edit(prepared["draft_dir"])
        Path(prepared["candidate"]).write_bytes(Path(prepared["candidate"]).read_bytes() + b"\nMore after review\n")
        before = self.snapshot()
        with self.assertRaisesRegex(project_rules.ProjectError, "审查后"):
            project_edit.apply_edit(prepared["draft_dir"], candidate_sha256=view["candidate_sha256"])
        self.assertEqual(before, self.snapshot())

    def test_manifest_newline_change_after_preview_cannot_change_applied_bytes(self):
        for apply_first in (False, True):
            with self.subTest(already_applied=apply_first):
                prepared = self.draft()
                self.candidate(prepared)
                view = project_edit.preview_edit(prepared["draft_dir"])
                original_candidate = Path(prepared["candidate"]).read_bytes()
                if apply_first:
                    project_edit.apply_edit(prepared["draft_dir"], candidate_sha256=view["candidate_sha256"])
                manifest_path = Path(prepared["manifest"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["newline"], "lf")
                manifest["newline"] = "crlf"
                manifest_path.write_bytes(project_rules._json_bytes(manifest))
                before = self.snapshot()
                backups = sorted((self.root / project_rules.BACKUP_DIR).iterdir())
                with self.assertRaisesRegex(project_rules.ProjectError, "行尾"):
                    project_edit.apply_edit(prepared["draft_dir"], candidate_sha256=view["candidate_sha256"])
                self.assertEqual(before, self.snapshot())
                self.assertEqual(original_candidate, Path(prepared["candidate"]).read_bytes())
                self.assertEqual(backups, sorted((self.root / project_rules.BACKUP_DIR).iterdir()))

    def test_invalid_candidates_refuse_without_mutation(self):
        prepared = self.draft()
        original = Path(prepared["candidate"]).read_bytes()
        mutations = [b"", b"\xff", b"```markdown\n" + original + b"```\n",
                     original + b"\n" + project_rules.BEGIN_TOKEN + b"\n",
                     original.replace(WORKER["model"].encode(), OTHER["model"].encode()),
                     original.replace(b"- reasoning effort: `medium`", b"- reasoning effort: `high`"),
                     original + b"x" * project_edit.MAX_DOCUMENT_BYTES,
                     original.replace(project_rules.END_TOKEN, b"inline " + project_rules.END_TOKEN)]
        before = self.snapshot()
        for candidate in mutations:
            with self.subTest(prefix=candidate[:20]):
                Path(prepared["candidate"]).write_bytes(candidate)
                with self.assertRaises(project_rules.ProjectError):
                    project_edit.preview_edit(prepared["draft_dir"])
                self.assertEqual(before, self.snapshot())

    def test_legacy_state_valid_and_invalid_custom_state_refused(self):
        original = self.state()
        project_rules.validate_state(original)
        self.assertNotIn("custom_rule", original)
        prepared = self.draft()
        self.apply(prepared)
        custom = self.state()
        project_rules.validate_state(custom)
        for bad in ("", "missing placeholders", project_rules.BEGIN_TOKEN.decode(),
                    custom["custom_rule"].replace("{model}", "hardcoded")):
            with self.subTest(bad=bad[:40]):
                value = dict(custom, custom_rule=bad)
                with self.assertRaises(project_rules.ProjectError):
                    project_rules.validate_state(value)

    def test_state_write_failure_rolls_back_document_and_keeps_older_backups(self):
        prepared = self.draft()
        self.candidate(prepared)
        before = self.snapshot()
        old_backups = sorted((self.root / project_rules.BACKUP_DIR).iterdir())
        atomic = project_rules._atomic_write
        def fail_state(path, data, mode):
            if Path(path) == self.root / project_rules.STATE_FILE:
                raise OSError("injected state write failure")
            return atomic(path, data, mode)
        with patch.object(project_rules, "_atomic_write", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "injected"):
                self.apply(prepared)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(old_backups, sorted((self.root / project_rules.BACKUP_DIR).iterdir()))

    def test_line_endings_and_file_modes_preserved(self):
        path = self.root / "AGENTS.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        state = self.state()
        state["sha256"] = project_rules._marker_info(path.read_bytes())["sha256"]
        (self.root / project_rules.STATE_FILE).write_bytes(project_rules._json_bytes(state))
        os.chmod(path, 0o640)
        expected_mode = path.stat().st_mode & 0o777
        prepared = self.draft()
        self.candidate(prepared, lambda raw: raw.replace(b"pytest", b"pytest -q").replace(b"\r\n", b"\n"))
        self.apply(prepared)
        raw = path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertEqual(path.stat().st_mode & 0o777, expected_mode)
        self.assertEqual(project_rules.sync_project(self.root, template_path=TEMPLATE)["result"], "unchanged")

    def test_preview_normalizes_block_to_renderable_snapshot_before_apply(self):
        prepared = self.draft()
        raw = self.candidate(prepared, lambda data: data.replace(
            project_rules.END_TOKEN, b"\n\n" + project_rules.END_TOKEN).rstrip(b"\n"))
        view = project_edit.preview_edit(prepared["draft_dir"])
        self.assertEqual(view["candidate_sha256"], hashlib.sha256(raw).hexdigest())
        self.apply(prepared)
        applied = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(applied.endswith(project_rules.END_TOKEN + b"\n"))
        self.assertEqual(project_rules.sync_project(self.root, template_path=TEMPLATE)["result"], "unchanged")
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), applied)

    def test_prepare_requires_initialized_enabled_effective_project(self):
        override = self.root / "AGENTS.override.md"
        override.write_bytes(b"# Override\n")
        with self.assertRaisesRegex(project_rules.ProjectError, "sync"):
            self.draft()
        override.unlink()
        project_rules.disable_project(self.root)
        with self.assertRaisesRegex(project_rules.ProjectError, "禁用"):
            self.draft()
        uninitialized = self.directory / "uninitialized"
        uninitialized.mkdir()
        subprocess.run(["git", "init", "--quiet", str(uninitialized)], check=True)
        self.assertEqual(project_rules.resolve_project_root(uninitialized, mutation=True), uninitialized)
        with self.assertRaisesRegex(project_rules.ProjectError, "尚未登记"):
            project_edit.prepare_edit(uninitialized)

    def test_project_atomic_staging_is_under_project_tmp_and_is_removed(self):
        actual = os.replace
        replacements = []
        def record(source, destination):
            source = Path(source)
            source.relative_to(self.root / "tmp")
            replacements.append(source)
            return actual(source, destination)
        prepared = self.draft()
        self.candidate(prepared)
        with patch.object(project_rules.os, "replace", side_effect=record):
            self.apply(prepared)
        self.assertTrue(replacements)
        self.assertTrue(all(not path.exists() for path in replacements))

    def test_symlinked_candidate_manifest_and_tmp_refused(self):
        self.require_symlinks()
        for file_name in ("candidate.md", "manifest.json"):
            prepared = self.draft()
            target = Path(prepared["draft_dir"]) / file_name
            outside = self.directory / ("outside-" + file_name)
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaises(project_rules.ProjectError):
                project_edit.preview_edit(prepared["draft_dir"])
        temporary_root = self.root / "tmp"
        moved = self.root / "actual-tmp"
        temporary_root.rename(moved)
        temporary_root.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(project_rules.ProjectError):
            self.draft()

    def test_draft_cannot_be_rebound_to_another_project(self):
        prepared = self.draft()
        manifest_path = Path(prepared["manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        other = self.directory / "other"
        other.mkdir()
        subprocess.run(["git", "init", "--quiet", str(other)], check=True)
        (other / "tmp").mkdir()
        manifest["project_root"] = str(other)
        manifest_path.write_bytes(project_rules._json_bytes(manifest))
        before = self.snapshot()
        with self.assertRaises(project_rules.ProjectError):
            project_edit.preview_edit(prepared["draft_dir"])
        self.assertEqual(before, self.snapshot())

    def test_large_valid_document_warns_of_context_limit(self):
        prepared = self.draft()
        self.candidate(prepared, lambda raw: b"# Long project\n" + b"Context detail\n" * 2600 + raw)
        self.assertTrue(project_edit.preview_edit(prepared["draft_dir"])["diagnostics"])

    def test_generation_failure_never_replaces_candidate_or_project(self):
        prepared = self.draft()
        before = self.snapshot()
        candidate_before = Path(prepared["candidate"]).read_bytes()
        failed = {"agent_id": "wk-test", "status": "failed", "error": "provider unavailable",
                  "error_category": "startup", "requested": {"model": WORKER["model"]},
                  "observed": {"status": "unverified"}}
        with patch.object(worker_runtime.Runtime, "spawn", return_value=failed), \
                patch.object(worker_runtime.Runtime, "shutdown"):
            with self.assertRaisesRegex(project_rules.ProjectError, "wk-test"):
                project_edit.generate_edit(prepared, self.directory / "home")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(candidate_before, Path(prepared["candidate"]).read_bytes())
        self.assertEqual(json.loads((Path(prepared["draft_dir"]) / "generation.json").read_bytes())["error_category"], "startup")

    def test_generation_uses_explicit_project_pair_and_readonly(self):
        prepared = self.draft()
        output = Path(prepared["candidate"]).read_bytes().replace(b"pytest before", b"pytest -q before")
        completed = {"agent_id": "wk-test", "status": "completed", "output": output.decode(),
                     "output_truncated": False, "requested": {"model": WORKER["model"],
                                                                "reasoning_effort": WORKER["reasoning_effort"]},
                     "observed": {"status": "unverified"}}
        before = self.snapshot()
        with patch.object(worker_runtime.Runtime, "spawn", return_value=completed) as spawn, \
                patch.object(worker_runtime.Runtime, "shutdown"):
            result = project_edit.generate_edit(prepared, self.directory / "home")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(Path(prepared["candidate"]).read_bytes(), output)
        self.assertEqual(result["agent_id"], "wk-test")
        self.assertEqual(result["observed"]["status"], "unverified")
        self.assertEqual(spawn.call_args.kwargs["model"], WORKER["model"])
        self.assertEqual(spawn.call_args.kwargs["effort"], WORKER["reasoning_effort"])
        self.assertEqual(spawn.call_args.kwargs["sandbox"], "read-only")

    def test_generation_bad_output_and_operation_error_leave_candidate(self):
        prepared = self.draft()
        before = self.snapshot()
        candidate_before = Path(prepared["candidate"]).read_bytes()
        for fail in (worker_runtime.OperationError("CLI unavailable"),
                     {"agent_id": "wk-test", "status": "completed", "output": "explanation only"}):
            with self.subTest(fail=str(fail)[:60]):
                with patch.object(worker_runtime.Runtime, "spawn",
                                  **({"side_effect": fail} if isinstance(fail, Exception)
                                     else {"return_value": fail})), \
                        patch.object(worker_runtime.Runtime, "shutdown"):
                    with self.assertRaises(project_rules.ProjectError):
                        project_edit.generate_edit(prepared, self.directory / "home")
                self.assertEqual(before, self.snapshot())
                self.assertEqual(candidate_before, Path(prepared["candidate"]).read_bytes())

    def test_generation_keyboard_interrupt_cancels_worker_without_applying(self):
        prepared = self.draft()
        before = self.snapshot()
        running = {"agent_id": "wk-test", "status": "running"}
        with patch.object(worker_runtime.Runtime, "spawn", return_value=running), \
                patch.object(worker_runtime.Runtime, "get", side_effect=KeyboardInterrupt), \
                patch.object(worker_runtime.Runtime, "cancel", return_value={**running, "status": "cancelled"}) as cancel, \
                patch.object(worker_runtime.Runtime, "shutdown") as shutdown:
            with self.assertRaises(KeyboardInterrupt):
                project_edit.generate_edit(prepared, self.directory / "home")
        cancel.assert_called_once_with("wk-test")
        shutdown.assert_called_once()
        self.assertEqual(before, self.snapshot())

    def test_generation_does_not_overwrite_candidate_changed_while_worker_runs(self):
        prepared = self.draft()
        before = self.snapshot()
        path = Path(prepared["candidate"])
        original = path.read_bytes()
        manually_updated = original + b"\n# Main agent's later edit\n"
        completed = {"agent_id": "wk-test", "status": "completed", "output": original.decode()}
        def finish(_agent_id):
            path.write_bytes(manually_updated)
            return completed
        with patch.object(worker_runtime.Runtime, "spawn", return_value={"agent_id": "wk-test", "status": "running"}), \
                patch.object(worker_runtime.Runtime, "get", side_effect=finish), \
                patch.object(worker_runtime.Runtime, "shutdown"):
            with self.assertRaisesRegex(project_rules.ProjectError, "生成期间"):
                project_edit.generate_edit(prepared, self.directory / "home")
        self.assertEqual(path.read_bytes(), manually_updated)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
