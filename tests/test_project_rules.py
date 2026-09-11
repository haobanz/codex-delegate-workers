import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
sys_path_added = str(SCRIPTS)
import sys
sys.path.insert(0, sys_path_added)
import manage
import project_rules
sys.path.pop(0)


TEMPLATE = ROOT / "skills/delegate-workers/references/project-delegation.md"
WORKER = {"model": "gpt-5.6-luna", "reasoning_effort": "medium"}
COMPLEX = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}


class ProjectRulesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory(prefix="delegate-project-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # The production resolver strips GIT_* variables deliberately. Reapply
        # a discovery boundary only in tests so non-Git fixtures stay isolated.
        clean_environment = project_rules._clean_git_environment
        boundary = patch.object(project_rules, "_clean_git_environment", side_effect=lambda: {
            **clean_environment(), "GIT_CEILING_DIRECTORIES": str(self.root)})
        boundary.start()
        self.addCleanup(boundary.stop)

    def project(self, name="project", *, git=True):
        path = self.root / name
        path.mkdir(parents=True)
        if git:
            subprocess.run(["git", "init", "--quiet", str(path)], check=True)
        return path

    def init(self, path, worker=WORKER, profile="default"):
        return project_rules.init_project(path, template_path=TEMPLATE,
                                          profile=profile, worker=worker)

    def read_state(self, path):
        return json.loads((path / project_rules.STATE_FILE).read_text(encoding="utf-8"))

    def require_symlinks(self):
        target = self.root / "symlink-probe-target"
        link = self.root / "symlink-probe-link"
        target.write_bytes(b"probe")
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            target.unlink(missing_ok=True)
            self.skipTest(f"symlink creation is unavailable: {exc}")
        link.unlink()
        target.unlink()

    def test_missing_existing_empty_and_all_line_endings_round_trip_exactly(self):
        originals = [None, b"", b"foo", b"foo\n", b"foo\r\n", b"foo\r",
                     b"foo\n\n", b"foo\r\n\r\n"]
        for index, original in enumerate(originals):
            with self.subTest(original=original):
                path = self.project(f"ending-{index}")
                agents = path / "AGENTS.md"
                if original is not None:
                    agents.write_bytes(original)
                self.init(path)
                project_rules.disable_project(path)
                if original is None:
                    self.assertFalse(agents.exists())
                else:
                    self.assertEqual(agents.read_bytes(), original)

    def test_idempotent_init_preserves_created_file_and_mtime(self):
        path = self.project()
        first = self.init(path)
        agents = path / "AGENTS.md"
        state = path / project_rules.STATE_FILE
        agents_mtime = agents.stat().st_mtime_ns
        state_mtime = state.stat().st_mtime_ns
        agents_bytes = agents.read_bytes()
        state_bytes = state.read_bytes()
        second = project_rules.init_project(path, template_path=TEMPLATE)
        self.assertEqual(second["result"], "unchanged")
        self.assertEqual(agents.read_bytes(), agents_bytes)
        self.assertEqual(state.read_bytes(), state_bytes)
        self.assertEqual(agents.stat().st_mtime_ns, agents_mtime)
        self.assertEqual(state.stat().st_mtime_ns, state_mtime)
        self.assertTrue(self.read_state(path)["created_file"])
        self.assertIsNotNone(first["backup"])
        self.assertIsNone(second["backup"])

    def test_user_bytes_outside_owned_block_are_preserved(self):
        path = self.project()
        agents = path / "AGENTS.md"
        agents.write_bytes(b"# before\r\n")
        self.init(path)
        with agents.open("ab") as stream:
            stream.write(b"\r\n# added later\r\n")
        before = agents.read_bytes()
        project_rules.init_project(path, template_path=TEMPLATE)
        after = agents.read_bytes()
        self.assertTrue(after.endswith(b"\r\n# added later\r\n"))
        self.assertIn(b"# before\r\n", after)
        self.assertEqual(after.count(project_rules.BEGIN_TOKEN), 1)
        self.assertEqual(after.count(project_rules.END_TOKEN), 1)
        self.assertEqual(before.split(project_rules.BEGIN_TOKEN)[0],
                         after.split(project_rules.BEGIN_TOKEN)[0])

    def test_owned_edit_and_orphan_fail_without_instruction_or_state_writes(self):
        path = self.project()
        self.init(path)
        agents = path / "AGENTS.md"
        state = path / project_rules.STATE_FILE
        agents.write_bytes(agents.read_bytes().replace(b"Project Worker Delegation",
                                                        b"User changed this block"))
        before = {p: p.read_bytes() for p in (agents, state)}
        with self.assertRaises(project_rules.ProjectError):
            project_rules.init_project(path, template_path=TEMPLATE)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

        orphan = self.project("orphan")
        orphan_agents = orphan / "AGENTS.md"
        orphan_agents.write_bytes(b"\n" + project_rules.BEGIN_TOKEN + b"\nunknown\n"
                                  + project_rules.END_TOKEN + b"\n")
        orphan_before = orphan_agents.read_bytes()
        with self.assertRaises(project_rules.ProjectError):
            self.init(orphan)
        self.assertEqual(orphan_agents.read_bytes(), orphan_before)
        self.assertFalse((orphan / project_rules.STATE_FILE).exists())

    def test_override_is_used_and_intact_block_migrates(self):
        path = self.project()
        base = path / "AGENTS.md"
        override = path / "AGENTS.override.md"
        base.write_bytes(b"# base\n")
        override.write_bytes(b"# override\n")
        result = self.init(path)
        self.assertEqual(result["file"], "AGENTS.override.md")
        self.assertEqual(base.read_bytes(), b"# base\n")
        self.assertIn(project_rules.BEGIN_TOKEN, override.read_bytes())
        status = project_rules.status_project(path)
        self.assertEqual(status["integrity"], "ok")

        project_rules.disable_project(path)
        self.assertEqual(override.read_bytes(), b"# override\n")
        self.assertEqual(base.read_bytes(), b"# base\n")

    def test_later_override_is_reported_and_sync_migrates_owned_block(self):
        path = self.project()
        base = path / "AGENTS.md"
        self.init(path)
        base_before = base.read_bytes()
        override = path / "AGENTS.override.md"
        override.write_bytes(b"# new override\n")
        status = project_rules.status_project(path)
        self.assertTrue(status["scope_warnings"])
        self.assertIn("override", " ".join(status["scope_warnings"]))
        synced = project_rules.sync_project(path, template_path=TEMPLATE)
        self.assertEqual(synced["file"], "AGENTS.override.md")
        self.assertFalse(base.exists())
        self.assertIn(project_rules.BEGIN_TOKEN, override.read_bytes())

    def test_sync_cannot_opt_in_new_or_disabled_project_and_reenable_preserves_snapshot(self):
        path = self.project()
        with self.assertRaises(project_rules.ProjectError):
            project_rules.sync_project(path, template_path=TEMPLATE)
        self.init(path, worker=COMPLEX, profile="complex")
        project_rules.disable_project(path)
        with self.assertRaises(project_rules.ProjectError):
            project_rules.sync_project(path, template_path=TEMPLATE)
        result = project_rules.init_project(path, template_path=TEMPLATE)
        self.assertEqual(result["worker"], COMPLEX)
        self.assertTrue(self.read_state(path)["enabled"])

    def test_invalid_state_status_is_read_only_and_does_not_traceback(self):
        path = self.project()
        state = path / project_rules.STATE_FILE
        state.write_text(json.dumps({"schema_version": 1, "enabled": True, "file": [],
                                     "sha256": "0" * 64, "created_file": False,
                                     "selection": {"profile": "default"},
                                     "worker": WORKER}), encoding="utf-8")
        before = state.read_bytes()
        status = project_rules.status_project(path)
        self.assertEqual(status["status"], "invalid_state")
        self.assertFalse(status["state_valid"])
        self.assertEqual(state.read_bytes(), before)
        self.assertNotIn(project_rules.LOCK_FILE, {p.name for p in path.iterdir()})

    def test_invalid_init_options_do_not_create_project_artifacts(self):
        source = self.root / "source"
        shutil.copytree(ROOT / "skills/delegate-workers", source / "skills/delegate-workers")
        home = self.root / "codex-home"
        home.mkdir()
        installation = manage.Installation(home)
        installation.install(source)
        path = self.project("invalid-options")
        before = {entry.name for entry in path.iterdir()}
        with self.assertRaises(ValueError):
            installation.project_init(path, model="", effort="medium")
        with self.assertRaises(ValueError):
            installation.project_init(path, effort="not-an-effort")
        self.assertEqual({entry.name for entry in path.iterdir()}, before)

    def test_unsupported_stored_pair_is_reported_but_can_be_disabled(self):
        path = self.project()
        self.init(path)
        state_path = path / project_rules.STATE_FILE
        state = self.read_state(path)
        state["worker"] = {"model": "gpt-5.6-luna", "reasoning_effort": "ultra"}
        state_path.write_bytes(json.dumps(state, indent=2).encode("utf-8") + b"\n")
        status = project_rules.status_project(path)
        self.assertEqual(status["compatibility"]["status"], "incompatible")
        disabled = project_rules.disable_project(path)
        self.assertFalse(disabled["enabled"])
        self.assertFalse((path / "AGENTS.md").exists())


    def test_nested_guidance_is_limited_to_requested_path(self):
        path = self.project()
        requested = path / "src" / "pkg"
        requested.mkdir(parents=True)
        (path / "src" / "AGENTS.md").write_text("nested", encoding="utf-8")
        (path / "other").mkdir()
        (path / "other" / "AGENTS.md").write_text("not in path", encoding="utf-8")
        status = project_rules.status_project(requested)
        nested = [entry["path"] for entry in status["nested_guidance"]]
        self.assertIn(str(path / "src" / "AGENTS.md"), nested)
        self.assertNotIn(str(path / "other" / "AGENTS.md"), nested)
        self.assertIn("嵌套", " ".join(status["scope_warnings"]))

    def test_worktree_root_and_explicit_non_git_rules(self):
        path = self.project()
        requested = path / "subdir"
        requested.mkdir()
        result = self.init(requested)
        self.assertEqual(Path(result["project_root"]), path)
        self.assertTrue((path / project_rules.STATE_FILE).exists())

        non_git = self.project("non-git", git=False)
        result = self.init(non_git)
        self.assertFalse(result["git_worktree"])
        self.assertEqual(Path(project_rules.status_project(non_git)["project_root"]), non_git)
        previous = os.getcwd()
        try:
            os.chdir(non_git)
            with self.assertRaises(project_rules.ProjectError):
                project_rules.init_project(None, template_path=TEMPLATE,
                                           profile="default", worker=WORKER)
        finally:
            os.chdir(previous)

        linked_source = self.project("linked-source")
        (linked_source / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(linked_source), "add", "README.md"], check=True)
        empty_hooks = self.root / "empty-hooks"
        empty_hooks.mkdir()
        subprocess.run(["git", "-C", str(linked_source), "-c", "user.name=Test User",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", f"core.hooksPath={empty_hooks}", "commit", "--quiet", "-m",
                        "initial"], check=True)
        linked = self.root / "linked-worktree"
        subprocess.run(["git", "-C", str(linked_source), "worktree", "add", "--quiet",
                        "--detach", str(linked), "HEAD"], check=True)
        self.assertTrue((linked / ".git").is_file())
        result = self.init(linked)
        self.assertEqual(Path(result["project_root"]), linked)

    def test_symlink_instruction_state_and_lock_are_refused(self):
        self.require_symlinks()
        path = self.project()
        target = self.root / "target-agents"
        target.write_bytes(b"user bytes")
        (path / "AGENTS.md").symlink_to(target)
        with self.assertRaises(project_rules.ProjectError):
            self.init(path)
        self.assertEqual(target.read_bytes(), b"user bytes")
        (path / "AGENTS.md").unlink()
        self.init(path)
        state_target = self.root / "target-state"
        state_target.write_bytes((path / project_rules.STATE_FILE).read_bytes())
        (path / project_rules.STATE_FILE).unlink()
        (path / project_rules.STATE_FILE).symlink_to(state_target)
        agents_before = (path / "AGENTS.md").read_bytes()
        with self.assertRaises(project_rules.ProjectError):
            project_rules.sync_project(path, template_path=TEMPLATE)
        self.assertEqual((path / "AGENTS.md").read_bytes(), agents_before)

        (path / project_rules.STATE_FILE).unlink()
        (path / project_rules.STATE_FILE).write_bytes(state_target.read_bytes())
        lock_target = self.root / "target-lock"
        lock_target.write_bytes(b"lock")
        (path / project_rules.LOCK_FILE).unlink()
        (path / project_rules.LOCK_FILE).symlink_to(lock_target)
        with self.assertRaises(project_rules.ProjectError):
            project_rules.disable_project(path)
        self.assertEqual(lock_target.read_bytes(), b"lock")

    def test_unsafe_root_and_backup_symlink_are_refused(self):
        with self.assertRaises(project_rules.ProjectError):
            project_rules.resolve_project_root(Path("/"), mutation=True)
        with self.assertRaises(project_rules.ProjectError):
            project_rules.resolve_project_root(Path.home(), mutation=True)

        self.require_symlinks()
        path = self.project()
        backup_target = self.root / "backup-target"
        backup_target.mkdir()
        (path / project_rules.BACKUP_DIR).symlink_to(backup_target, target_is_directory=True)
        with self.assertRaises(project_rules.ProjectError):
            self.init(path)
        self.assertFalse((path / project_rules.STATE_FILE).exists())
        self.assertFalse((path / "AGENTS.md").exists())

    def test_project_lock_is_shared_across_different_codex_homes(self):
        path = self.project()
        self.init(path)
        home_one = self.root / "home-one"
        home_two = self.root / "home-two"
        home_one.mkdir()
        home_two.mkdir()
        installation = manage.Installation(home_two)
        manage.Installation(home_one)
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import project_rules\n"
            "from pathlib import Path\n"
            "with project_rules.project_lock(Path(sys.argv[1])):\n"
            "    print('ready', flush=True)\n"
            "    input()\n"
        )
        holder = subprocess.Popen([sys.executable, "-B", "-c", script, str(path), str(SCRIPTS)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            with self.assertRaises(project_rules.ProjectError):
                installation.project_disable(path)
        finally:
            if holder.stdin:
                holder.stdin.write("\n")
                holder.stdin.flush()
            holder.wait(timeout=10)
            if holder.stdin:
                holder.stdin.close()
            if holder.stdout:
                holder.stdout.close()
            if holder.stderr:
                holder.stderr.close()

    def test_transaction_failure_rolls_back_instruction_and_state(self):
        path = self.project()
        self.init(path)
        agents = path / "AGENTS.md"
        state = path / project_rules.STATE_FILE
        before_agents = agents.read_bytes()
        before_state = state.read_bytes()
        changed_template = self.root / "changed-template.md"
        changed_template.write_bytes(TEMPLATE.read_bytes() + b"\nExtra revision.\n")
        original_write = project_rules._atomic_write

        def fail_state_write(write_path, data, mode):
            if Path(write_path) == state:
                raise OSError("simulated state write failure")
            return original_write(write_path, data, mode)

        with patch.object(project_rules, "_atomic_write", side_effect=fail_state_write), \
                self.assertRaises(OSError):
            project_rules.sync_project(path, template_path=changed_template)
        self.assertEqual(agents.read_bytes(), before_agents)
        self.assertEqual(state.read_bytes(), before_state)

    def test_status_is_read_only_and_reports_unknown_runtime_state(self):
        path = self.project()
        before = {entry.name for entry in path.iterdir()}
        status = project_rules.status_project(path)
        after = {entry.name for entry in path.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(status["integrity"], "not_registered")
        self.assertFalse(status["file_present"])
        self.assertIsNone(status["session_loaded"])
        self.assertFalse(status["runtime_verified"])

    def test_installed_configuration_snapshot_survives_global_reconfigure(self):
        source = self.root / "source"
        shutil.copytree(ROOT / "skills/delegate-workers", source / "skills/delegate-workers")
        home = self.root / "codex-home"
        home.mkdir()
        installation = manage.Installation(home)
        installation.install(source)
        project = self.project("configured")
        installation.project_init(project, profile="complex")
        installation.configure("default", model="gpt-5.6-luna", effort="max")
        synced = installation.project_sync(project)
        self.assertEqual(synced["worker"], COMPLEX)
        changed = installation.project_init(project, effort="low")
        self.assertEqual(changed["worker"], {"model": "gpt-5.6-terra", "reasoning_effort": "low"})

    def test_installed_cli_project_lifecycle_smoke(self):
        source = self.root / "source"
        shutil.copytree(ROOT / "skills/delegate-workers", source / "skills/delegate-workers")
        home = self.root / "codex-home"
        home.mkdir()
        installation = manage.Installation(home)
        installation.install(source)
        project = self.project("cli")
        command = installation.command_dir / ("dw.cmd" if os.name == "nt" else "dw")
        environment = {**os.environ, "PATH": str(installation.command_dir) + os.pathsep + os.environ.get("PATH", "")}
        init = subprocess.run([str(command), "project", "init", "--path", str(project),
                               "--profile", "complex"], cwd=self.root, env=environment,
                              capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(init.stdout)["worker"], COMPLEX)
        status = subprocess.run([str(command), "project", "status", "--path", str(project)],
                                cwd=self.root, env=environment, capture_output=True,
                                text=True, encoding="utf-8", check=True)
        status_value = json.loads(status.stdout)
        self.assertEqual(status_value["integrity"], "ok")
        disabled = subprocess.run([str(command), "project", "disable", "--path", str(project)],
                                  cwd=self.root, env=environment, capture_output=True,
                                  text=True, encoding="utf-8", check=True)
        self.assertFalse(json.loads(disabled.stdout)["enabled"])


if __name__ == "__main__":
    unittest.main()
