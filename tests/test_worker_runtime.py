"""worker_runtime / cli_support 的真实行为测试（使用明确的假 CLI，无网络调用）。"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
FAKE_CLI = ROOT / "tests/fixtures/fake_codex.py"
TEMPLATE = ROOT / "skills/delegate-workers/references/project-delegation.md"
sys.path.insert(0, str(SCRIPTS))
import cli_support  # noqa: E402
import platform_support  # noqa: E402
import project_rules  # noqa: E402
import worker_runtime  # noqa: E402
sys.path.pop(0)


SNAPSHOT_WORKER = {"model": "deepseek/deepseek-v4.1-flash", "reasoning_effort": "max"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def process_alive(pid):
    if pid is None:
        return False
    if os.name == "nt":  # pragma: no cover - 仅在 Windows 上执行
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True
    return True


class RuntimeCase(unittest.TestCase):
    """公共夹具：隔离的 CODEX_HOME、git 项目根、假 CLI 命令前缀。"""

    def setUp(self):
        self.temporary = temporary_directory(prefix="delegate-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        clean = project_rules._clean_git_environment
        boundary = patch.object(project_rules, "_clean_git_environment", side_effect=lambda: {
            **clean(), "GIT_CEILING_DIRECTORIES": str(self.root)})
        boundary.start()
        self.addCleanup(boundary.stop)
        self.home = self.root / "codex-home"
        self.config_path = self.home / "skills/delegate-workers/workers.json"
        self.write_workers({"default": {"model": "gpt-5.6-luna", "reasoning_effort": "medium"},
                            "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "high"}},
                           default="default")
        self.project = self.make_git_project("project")
        self.runtimes = []
        for name in list(os.environ):
            if name.startswith("FAKE_CODEX_"):
                self.addCleanup(os.environ.pop, name, None)

    def write_workers(self, profiles, default="default"):
        write_json(self.config_path, {"version": 2, "default_profile": default, "profiles": profiles})

    def make_git_project(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def register(self, path, worker=SNAPSHOT_WORKER, profile="default"):
        return project_rules.init_project(path, template_path=TEMPLATE, profile=profile, worker=worker)

    def runtime(self, command_prefix=None, **kwargs):
        instance = worker_runtime.Runtime(
            codex_home=self.home,
            command_prefix=command_prefix or [sys.executable, str(FAKE_CLI)],
            **kwargs)
        self.runtimes.append(instance)
        self.addCleanup(instance.shutdown)
        return instance

    # -- 辅助 -------------------------------------------------------------

    def spawn(self, runtime, *, cwd=None, task="do the thing", env=None, **kwargs):
        target = str(cwd or self.project)
        with patch.dict(os.environ, env or {}, clear=False):
            return runtime.spawn(task=task, cwd=target, **kwargs)

    def wait_terminal(self, runtime, agent_id, timeout=40.0):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = runtime.get(agent_id)
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        self.fail(f"worker 未在 {timeout}s 内进入终态：{state and state['status']}")

    def job_dir(self, state):
        return Path(state["artifacts"]["job_dir"])


class CliSupportTests(RuntimeCase):
    def test_injected_path_must_be_executable(self):
        with self.assertRaises(cli_support.CliNotFoundError) as caught:
            cli_support.codex_command(self.root / "missing-codex")
        self.assertIn("不可执行", str(caught.exception))

    def test_path_lookup_returns_resolved_executable(self):
        binary = self.root / "bin with space" / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        with patch.object(cli_support.shutil, "which", return_value=str(binary)):
            self.assertEqual(cli_support.codex_command(), [str(binary.resolve())])

    def test_missing_executable_error_is_actionable(self):
        with patch.object(cli_support.shutil, "which", return_value=None), \
                patch.object(cli_support, "_posix_candidates", return_value=[]), \
                patch.object(cli_support.platform_support, "WINDOWS", False):
            with self.assertRaises(cli_support.CliNotFoundError) as caught:
                cli_support.codex_command()
        self.assertIn("Codex CLI", str(caught.exception))

    def test_posix_npm_javascript_entry_uses_node(self):
        script = self.root / "global/bin/codex"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n// codex cli\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        node = self.root / "global/bin/node"
        node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        node.chmod(node.stat().st_mode | stat.S_IXUSR)
        with patch.object(cli_support.platform_support, "WINDOWS", False), \
                patch.object(cli_support, "_posix_candidates", return_value=[script]), \
                patch.object(cli_support.shutil, "which",
                             side_effect=lambda name: str(node) if name == "node" else None):
            self.assertEqual(cli_support.codex_command(), [str(node.resolve()), str(script.resolve())])

    def test_posix_npm_entry_without_node_is_refused(self):
        script = self.root / "global/bin/codex"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with patch.object(cli_support.platform_support, "WINDOWS", False), \
                patch.object(cli_support, "_posix_candidates", return_value=[script]), \
                patch.object(cli_support.shutil, "which", return_value=None):
            with self.assertRaises(cli_support.CliNotFoundError) as caught:
                cli_support.codex_command()
        self.assertIn("node", str(caught.exception))

    def test_windows_shim_uses_node_without_shell(self):
        shim = self.root / "npm/codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo off\r\n", encoding="utf-8")
        script = shim.parent / "node_modules/@openai/codex/bin/codex.js"
        script.parent.mkdir(parents=True)
        script.write_text("// codex\n", encoding="utf-8")
        node = shim.parent / "node.exe"
        node.write_bytes(b"MZ")
        with patch.object(cli_support.platform_support, "WINDOWS", True), \
                patch.object(cli_support, "_windows_candidates", return_value=[shim]), \
                patch.object(cli_support, "_windows_fallback_executables", return_value=[node]):
            command = cli_support.codex_command()
        self.assertEqual(command, [str(node), str(script)])
        self.assertNotIn("cmd", " ".join(command).lower().replace(".cmd", ""))

    def test_windows_shim_without_node_is_refused(self):
        shim = self.root / "npm/codex.cmd"
        shim.parent.mkdir(parents=True)
        shim.write_text("@echo off\r\n", encoding="utf-8")
        with patch.object(cli_support.platform_support, "WINDOWS", True), \
                patch.object(cli_support, "_windows_candidates", return_value=[shim]), \
                patch.object(cli_support, "_windows_fallback_executables", return_value=[]):
            with self.assertRaises(cli_support.CliNotFoundError) as caught:
                cli_support.codex_command()
        self.assertIn("shell", str(caught.exception))


class CommandBuildTests(RuntimeCase):
    def test_custom_model_and_effort_reach_argv_unchanged(self):
        runtime = self.runtime()
        command = runtime.build_command(model="vendor/custom-model:1", effort="ultra",
                                       sandbox="workspace-write",
                                       last_message_path=self.root / "last.txt",
                                       git_worktree=False)
        self.assertEqual(command[:2], [sys.executable, str(FAKE_CLI)])
        self.assertEqual(command[2:], [
            "exec", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "--model", "vendor/custom-model:1",
            "-c", 'model_reasoning_effort="ultra"',
            "--color", "never",
            "--output-last-message", str(self.root / "last.txt"),
            "-",
        ])
        self.assertNotIn("resume", command)
        self.assertIsInstance(command, list)

    def test_git_project_keeps_the_cli_repository_check(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        command = json.loads(Path(state["artifacts"]["request"]).read_text(encoding="utf-8"))["command"]
        self.assertNotIn("--skip-git-repo-check", command)
        self.wait_terminal(runtime, state["agent_id"])

    def test_command_never_enables_dangerous_flags(self):
        runtime = self.runtime()
        command = runtime.build_command(model="vendor/x", effort="max", sandbox="read-only",
                                       last_message_path=self.root / "last.txt",
                                       git_worktree=True)
        for flag in ("--dangerously-bypass-approvals-and-sandbox", "--full-auto",
                     "--approve-for-me", "--ignore-user-config", "resume", "fork"):
            self.assertNotIn(flag, command)

    def test_worker_sees_exact_argv_and_requested_pair(self):
        runtime = self.runtime()
        argv_copy = self.root / "argv.json"
        state = self.spawn(runtime, model="vendor/custom-model:1", effort="max",
                           env={"FAKE_CODEX_ARGV_COPY": str(argv_copy)})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        argv = json.loads(argv_copy.read_text(encoding="utf-8"))
        self.assertIn("vendor/custom-model:1", argv)
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertEqual(argv[argv.index("--model") + 1], "vendor/custom-model:1")
        self.assertEqual(final["resolved"]["model"], "vendor/custom-model:1")
        self.assertEqual(final["resolved"]["reasoning_effort"], "max")
        self.assertEqual(final["resolved"]["source"], "explicit")
        self.assertEqual(final["resolved"]["compatibility"]["status"], "unverified")

    def test_model_without_effort_is_rejected_before_start(self):
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.ContractError) as caught:
            runtime.spawn(task="x", cwd=str(self.project), model="vendor/custom-model")
        self.assertIn("reasoning_effort", str(caught.exception))
        self.assertEqual(runtime.list()["count"], 0)


class ResolutionTests(RuntimeCase):
    def test_project_snapshot_wins_over_global_default(self):
        project = self.make_git_project("registered")
        self.register(project)
        runtime = self.runtime()
        state = self.spawn(runtime, cwd=project)
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["resolved"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(final["resolved"]["reasoning_effort"], "max")
        self.assertEqual(final["resolved"]["source"], "project-snapshot")
        self.assertEqual(final["project_root"], str(project.resolve()))
        self.assertTrue(final["artifacts"]["job_dir"].count(str(project / "tmp")) > 0)

    def test_unregistered_project_uses_global_default(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-luna")
        self.assertEqual(final["resolved"]["reasoning_effort"], "medium")
        self.assertEqual(final["resolved"]["source"], "workers.json:default_profile")

    def test_disabled_project_uses_global_default(self):
        project = self.make_git_project("disabled")
        self.register(project)
        project_rules.disable_project(project)
        runtime = self.runtime()
        state = self.spawn(runtime, cwd=project)
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["resolved"]["source"], "workers.json:default_profile")
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-luna")

    def test_corrupt_project_state_is_not_silently_replaced(self):
        project = self.make_git_project("corrupt")
        self.register(project)
        (project / project_rules.STATE_FILE).write_text("{not json", encoding="utf-8")
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.ContractError) as caught:
            runtime.spawn(task="x", cwd=str(project))
        self.assertIn("拒绝静默改用全局默认值", str(caught.exception))
        self.assertEqual(runtime.list()["count"], 0)

    def test_drifted_instruction_block_is_not_silently_replaced(self):
        project = self.make_git_project("drift")
        self.register(project)
        agents = project / "AGENTS.md"
        original = agents.read_text(encoding="utf-8")
        self.assertIn("keeps its current model", original)
        agents.write_text(original.replace("keeps its current model",
                                           "keeps its current model (edited by a human)"),
                          encoding="utf-8")
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.ContractError):
            runtime.spawn(task="x", cwd=str(project))

    def test_explicit_profile_selects_configured_pair(self):
        runtime = self.runtime()
        state = self.spawn(runtime, profile="complex")
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-terra")
        self.assertEqual(final["resolved"]["reasoning_effort"], "high")
        self.assertEqual(final["resolved"]["source"], "workers.json:profile")

    def test_effort_only_override_changes_selected_pair(self):
        runtime = self.runtime()
        state = self.spawn(runtime, profile="complex", effort="max")
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-terra")
        self.assertEqual(final["resolved"]["reasoning_effort"], "max")
        self.assertEqual(final["resolved"]["source"], "workers.json:profile+effort-override")

    def test_unknown_profile_is_rejected(self):
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.ContractError) as caught:
            runtime.spawn(task="x", cwd=str(self.project), profile="nope")
        self.assertIn("未知的模型预设", str(caught.exception))

    def test_installed_workers_json_changes_are_reread(self):
        runtime = self.runtime()
        first = self.spawn(runtime)
        first_final = self.wait_terminal(runtime, first["agent_id"])
        self.assertEqual(first_final["resolved"]["model"], "gpt-5.6-luna")
        self.write_workers({"default": {"model": "vendor/next-model", "reasoning_effort": "low"},
                            "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "high"}})
        second = self.spawn(runtime)
        second_final = self.wait_terminal(runtime, second["agent_id"])
        self.assertEqual(second_final["resolved"]["model"], "vendor/next-model")
        self.assertEqual(second_final["resolved"]["reasoning_effort"], "low")

    def test_overrides_do_not_write_settings(self):
        project = self.make_git_project("settings")
        self.register(project)
        before = (project / project_rules.STATE_FILE).read_bytes()
        config_before = self.config_path.read_bytes()
        runtime = self.runtime()
        state = self.spawn(runtime, cwd=project, model="vendor/one-off", effort="high")
        self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual((project / project_rules.STATE_FILE).read_bytes(), before)
        self.assertEqual(self.config_path.read_bytes(), config_before)

    def test_list_models_reports_configured_preferences_and_scope(self):
        project = self.make_git_project("listed")
        self.register(project)
        runtime = self.runtime()
        payload = runtime.list_models(str(project))
        self.assertEqual(payload["default"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(payload["default"]["source"], "project-snapshot")
        self.assertFalse(payload["hardcoded_native_model_enum"])
        self.assertIn("不是供应商可用模型目录", payload["catalog_scope"])
        self.assertIn("complex", payload["profiles"])
        self.assertEqual(payload["profiles"]["complex"]["model"], "gpt-5.6-terra")
        self.assertEqual(payload["profiles"]["complex"]["compatibility"]["status"], "compatible")

    def test_list_models_surfaces_corrupt_project_instead_of_global_default(self):
        project = self.make_git_project("listed-corrupt")
        self.register(project)
        (project / project_rules.STATE_FILE).write_text("[]", encoding="utf-8")
        runtime = self.runtime()
        payload = runtime.list_models(str(project))
        self.assertIsNone(payload["default"])
        self.assertIn("拒绝静默改用全局默认值", payload["default_error"])
        self.assertIn("default", payload["profiles"])


class HeaderParserTests(unittest.TestCase):
    """针对真实 codex-cli 0.154.0 启动头部格式的解析检查（固定样本，非现场日志）。"""

    REAL_SHAPE = [
        "2026-09-15T11:33:58.456243Z ERROR codex_models_manager::manager: failed to refresh "
        "available models: unexpected status 403 Forbidden, url: https://example.invalid/models\n",
        "OpenAI Codex v0.154.0\n",
        "--------\n",
        "workdir: /home/user/project with space\n",
        "model: deepseek/deepseek-v4.1-flash\n",
        "provider: custom\n",
        "approval: on-request\n",
        "sandbox: read-only\n",
        "reasoning effort: max\n",
        "reasoning summaries: none\n",
        "session id: 01a0a4d8-5e38-77c2-b045-f7a90cae0c0f\n",
        "--------\n",
        "user\n",
        "task text mentioning model: something-else\n",
        "--------\n",
        "model: spoofed/after-prompt\n",
        "reasoning effort: low\n",
    ]

    def scan(self, lines):
        scan = worker_runtime.HeaderScan()
        for line in lines:
            scan.feed(line)
        return scan

    def test_records_real_header_and_ignores_everything_after_prompt(self):
        scan = self.scan(self.REAL_SHAPE)
        self.assertTrue(scan.done())
        self.assertEqual(scan.status, "recorded")
        self.assertEqual(scan.values["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(scan.values["reasoning effort"], "max")
        self.assertEqual(scan.values["session id"], "01a0a4d8-5e38-77c2-b045-f7a90cae0c0f")
        self.assertEqual(scan.values["provider"], "custom")

    def test_header_without_effort_is_unverified(self):
        lines = [line for line in self.REAL_SHAPE if not line.startswith("reasoning effort")]
        scan = self.scan(lines)
        self.assertEqual(scan.status, "unverified")
        self.assertIn("缺少字段", scan.reason)

    def test_unknown_cli_banner_never_yields_metadata(self):
        scan = self.scan(["Totally different CLI 3.0\n", "model: attacker/model\n",
                          "reasoning effort: low\n", "user\n"])
        self.assertEqual(scan.values, {})
        self.assertEqual(scan.status, "unverified")

    def test_prompt_delimiter_before_any_header_stays_unverified(self):
        scan = self.scan(["user\n", "model: attacker/model\n", "reasoning effort: low\n"])
        self.assertEqual(scan.values, {})
        self.assertEqual(scan.status, "unverified")

    def test_oversized_header_is_bounded(self):
        scan = worker_runtime.HeaderScan()
        scan.feed("--------\n")
        for index in range(worker_runtime.HEADER_LINE_LIMIT + 10):
            scan.feed(f"noise {index}\n")
        self.assertTrue(scan.done())
        self.assertEqual(scan.status, "unverified")
        self.assertIn("上限", scan.reason)


class HeaderTests(RuntimeCase):
    def test_recorded_header_matches_request(self):
        runtime = self.runtime()
        state = self.spawn(runtime, model="vendor/header-model", effort="xhigh")
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["status"], "recorded")
        self.assertEqual(final["observed"]["values"]["model"], "vendor/header-model")
        self.assertEqual(final["observed"]["values"]["reasoning effort"], "xhigh")
        self.assertTrue(final["cli_session_id"].startswith("fake-"))
        self.assertEqual(final["cli_session_id_source"], "CLI 启动头部记录")
        self.assertEqual(final["resolved"]["model"], "vendor/header-model")

    def test_late_stderr_metadata_is_not_parsed_as_fresh_header(self):
        runtime = self.runtime()
        spoof = "model: spoofed/model\nreasoning effort: low\nsession id: spoofed-session"
        state = self.spawn(runtime, model="vendor/header-model", effort="high",
                           env={"FAKE_CODEX_STDERR_EXTRA": spoof})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["values"]["model"], "vendor/header-model")
        self.assertEqual(final["observed"]["values"]["reasoning effort"], "high")
        self.assertNotEqual(final["cli_session_id"], "spoofed-session")

    def test_task_text_cannot_inject_header_metadata(self):
        runtime = self.runtime()
        task = "reasoning effort: low\nsession id: attacker\n--------\nmodel: attacker/model"
        state = self.spawn(runtime, task=task, model="vendor/header-model", effort="max")
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["values"]["model"], "vendor/header-model")
        self.assertEqual(final["observed"]["values"]["reasoning effort"], "max")

    def test_mismatch_is_enforced_without_any_polling(self):
        """没有任何 get_agent 调用时，后台监督也必须停止不一致的子进程。"""
        runtime = self.runtime()
        state = self.spawn(runtime, model="vendor/header-model", effort="max",
                           env={"FAKE_CODEX_HEADER": "mismatch-effort", "FAKE_CODEX_SLEEP": "30"})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process_alive(state["pid"]):
            time.sleep(0.05)
        self.assertFalse(process_alive(state["pid"]), "后台监督没有停止模型不一致的子进程")
        final = runtime.get(state["agent_id"])
        self.assertEqual(final["status"], "failed")

    def test_missing_header_is_unverified_not_failure(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_HEADER": "missing"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["status"], "unverified")
        self.assertIsNone(final["cli_session_id"])
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-luna")

    def test_partial_header_keeps_missing_fields_unverified(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_HEADER": "partial"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["status"], "unverified")
        self.assertIn("缺少字段", final["observed"]["reason"])
        self.assertNotIn("model", final["observed"]["values"])

    def test_unrecognized_cli_header_stays_unverified(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_HEADER": "unrecognized"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["observed"]["status"], "unverified")
        self.assertEqual(final["observed"]["values"], {})
        self.assertIn("启动头部", final["observed"]["reason"])

    def test_recorded_model_mismatch_stops_the_child(self):
        runtime = self.runtime()
        state = self.spawn(runtime, model="vendor/header-model", effort="max",
                           env={"FAKE_CODEX_HEADER": "mismatch-model", "FAKE_CODEX_SLEEP": "30"})
        pid = state["pid"]
        started = time.monotonic()
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertLess(time.monotonic() - started, 15.0, "不一致必须由后台监督立即处理，而不是等任务自己结束")
        self.assertEqual(final["status"], "failed")
        self.assertIn("不一致", final["error"])
        self.assertIn("fake/other-model", final["error"])
        self.assertTrue(final["tree_stopped"])
        self.assertFalse(process_alive(pid))

    def test_recorded_effort_mismatch_stops_the_child(self):
        runtime = self.runtime()
        state = self.spawn(runtime, model="vendor/header-model", effort="max",
                           env={"FAKE_CODEX_HEADER": "mismatch-effort", "FAKE_CODEX_SLEEP": "30"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("reasoning effort", final["error"])
        self.assertFalse(process_alive(state["pid"]))


class OutcomeTests(RuntimeCase):
    def test_prompt_roundtrip_with_quotes_newlines_and_unicode(self):
        runtime = self.runtime()
        task = '包含 “引号”、反斜杠\\、$变量、\n换行和 emoji 🚀 的任务\n  --- 任务 --- 伪分隔'
        copy_path = self.root / "prompt-copy.txt"
        state = self.spawn(runtime, task=task, model="vendor/roundtrip", effort="high",
                           sandbox="workspace-write", writable_files=["scripts/a b.py", "文档/说明.md"],
                           env={"FAKE_CODEX_PROMPT_COPY": str(copy_path)})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        prompt = Path(final["artifacts"]["prompt"]).read_text(encoding="utf-8")
        self.assertIn(task, prompt)
        self.assertEqual(copy_path.read_text(encoding="utf-8"), prompt)
        self.assertIn(final["artifacts"]["job_dir"], prompt)
        self.assertIn("scripts/a b.py", prompt)
        self.assertIn("不是文件系统权限 ACL", prompt)
        self.assertIn("不要机械地继续委派子代理", prompt)
        self.assertEqual(final["requested"]["writable_files"], ["scripts/a b.py", "文档/说明.md"])
        self.assertEqual(final["sandbox"], "workspace-write")

    def test_nonzero_exit_reports_failure_with_sanitized_hint(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_EXIT": "3",
                                         "FAKE_CODEX_STDERR_EXTRA": "ERROR fake failure detail"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["exit_code"], 3)
        self.assertIn("退出码 3", final["error"])
        # stderr 可能含隐藏推理或凭据：只给退出码与证据路径，绝不回传原文。
        self.assertNotIn("fake failure detail", final["error"])
        self.assertNotIn("OpenAI Codex v0.0.0-fake", final["error"])
        self.assertTrue(Path(final["artifacts"]["stderr_log"]).is_file())

    def test_missing_final_output_is_failure(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_WRITE": "0"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["exit_code"], 0)
        self.assertIn("--output-last-message 最终输出", final["error"])
        self.assertEqual(final["output"], "")

    def test_successful_run_exposes_output_and_artifacts(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["cli_session_id"])
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertIn("FAKE 最终回答", final["output"])
        job = self.job_dir(final)
        for name in ("request.json", "prompt.txt", "worker.stdout.log", "worker.stderr.log",
                     "last-message.txt", "result.json"):
            self.assertTrue((job / name).is_file(), name)
        self.assertIn("独立进程", final["backend"])
        self.assertIn("不代表供应商上游的物理模型身份", final["identity_note"])
        self.assertFalse(final["exit_code"])

    def test_request_artifacts_never_record_tokens_or_raw_stderr(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        final = self.wait_terminal(runtime, state["agent_id"])
        request = json.loads(Path(final["artifacts"]["request"]).read_text(encoding="utf-8"))
        self.assertFalse(request["route"]["aliases"])
        self.assertIn("未复制凭据", request["route"]["provider"])
        self.assertIn("不是", request["writable_files_note"])
        self.assertNotIn("API_KEY", json.dumps(request))
        self.assertNotIn("OpenAI Codex", json.dumps(final))


class ProcessControlTests(RuntimeCase):
    def test_repeated_spawns_are_distinct_fresh_processes(self):
        runtime = self.runtime()
        first = self.spawn(runtime, task="same task")
        second = self.spawn(runtime, task="same task")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertNotEqual(first["pid"], second["pid"])
        first_final = self.wait_terminal(runtime, first["agent_id"])
        second_final = self.wait_terminal(runtime, second["agent_id"])
        self.assertEqual(first_final["status"], "completed")
        self.assertEqual(second_final["status"], "completed")
        self.assertNotIn("resume", " ".join(json.loads(
            Path(first_final["artifacts"]["request"]).read_text(encoding="utf-8"))["command"]))
        self.assertEqual(runtime.list()["count"], 2)

    def test_cancel_terminates_process_and_descendants(self):
        runtime = self.runtime()
        child_pid_file = self.root / "child.pid"
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30",
                                         "FAKE_CODEX_SPAWN_CHILD": "1",
                                         "FAKE_CODEX_CHILD_PID_FILE": str(child_pid_file)})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not child_pid_file.is_file():
            time.sleep(0.05)
        self.assertTrue(child_pid_file.is_file(), "假 CLI 未写出子进程 PID")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(process_alive(state["pid"]))
        self.assertTrue(process_alive(child_pid))
        result = runtime.cancel(state["agent_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["tree_stopped"])
        self.assertFalse(process_alive(state["pid"]))
        self.assertFalse(process_alive(child_pid))
        self.assertIn("已确认 worker 进程树停止", result["cancel_note"])

    def test_cancel_forces_remaining_descendants_after_leader_exits(self):
        """组长收到 SIGTERM 退出后，仍然存活的子孙进程也必须被强制终止。"""
        runtime = self.runtime()
        child_pid_file = self.root / "stubborn.pid"
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30",
                                         "FAKE_CODEX_STUBBORN_CHILD": "1",
                                         "FAKE_CODEX_CHILD_PID_FILE": str(child_pid_file)})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not child_pid_file.is_file():
            time.sleep(0.05)
        self.assertTrue(child_pid_file.is_file(), "假 CLI 未登记顽固子进程 PID")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(process_alive(child_pid))
        result = runtime.cancel(state["agent_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["tree_stopped"])
        self.assertFalse(process_alive(state["pid"]))
        self.assertFalse(process_alive(child_pid), "残留子孙进程没有被强制终止")

    def test_cancel_marks_terminal_only_after_process_exit(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30"})
        result = runtime.cancel(state["agent_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["tree_stopped"])
        self.assertFalse(process_alive(state["pid"]))
        self.assertIsNotNone(result["finished_at"])
        self.assertIn("退出码", result["error"])

    def test_late_cancel_keeps_real_completion(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        done = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(done["status"], "completed")
        late = runtime.cancel(state["agent_id"])
        self.assertEqual(late["status"], "completed")
        self.assertIn("已处于终态", late["cancel_note"])
        self.assertEqual(late["output"], done["output"])

    def test_late_cancel_keeps_real_failure(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_EXIT": "4"})
        self.wait_terminal(runtime, state["agent_id"])
        late = runtime.cancel(state["agent_id"])
        self.assertEqual(late["status"], "failed")
        self.assertEqual(late["exit_code"], 4)

    def test_shutdown_stops_owned_children(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30"})
        self.assertTrue(process_alive(state["pid"]))
        runtime.shutdown()
        self.assertFalse(process_alive(state["pid"]))
        final = runtime.get(state["agent_id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertTrue(final["tree_stopped"])

    def test_unknown_agent_id_is_reported_as_agent_error(self):
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.AgentError):
            runtime.get("wk-nope")
        with self.assertRaises(worker_runtime.AgentError):
            runtime.cancel("wk-nope")
        with self.assertRaises(worker_runtime.AgentError):
            runtime.get(None)


class SafetyRegressionTests(RuntimeCase):
    """主代理三项复现与相关边界：独立会话后代、隐私、无全局回退、截断与早退竞态。"""

    def test_cancel_stops_detached_session_descendant(self):
        """后代自行 setsid 后不在组长进程组内，只能按采样到的身份终止。"""
        runtime = self.runtime()
        child_pid_file = self.root / "detached.pid"
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30",
                                         "FAKE_CODEX_DETACHED_CHILD": "1",
                                         "FAKE_CODEX_CHILD_PID_FILE": str(child_pid_file)})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not child_pid_file.is_file():
            time.sleep(0.05)
        self.assertTrue(child_pid_file.is_file(), "假 CLI 未登记独立会话子进程 PID")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(process_alive(child_pid))
        result = runtime.cancel(state["agent_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["tree_stopped"])
        self.assertFalse(process_alive(child_pid), "取消后独立会话后代仍然存活")

    def test_parent_exit_with_inherited_stderr_child_still_finalizes(self):
        """组长退出但后代仍持有 stderr 管道时，监督不能等到管道关闭才收尾。"""
        runtime = self.runtime()
        child_pid_file = self.root / "inherited.pid"
        state = self.spawn(runtime, env={"FAKE_CODEX_INHERIT_STDERR_CHILD": "1",
                                         "FAKE_CODEX_CHILD_PID_FILE": str(child_pid_file)})
        final = self.wait_terminal(runtime, state["agent_id"], timeout=40.0)
        self.assertFalse(process_alive(state["pid"]))
        self.assertIn(final["status"], ("completed", "failed", "cancelled"))
        if child_pid_file.is_file():
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            self.assertFalse(process_alive(child_pid), "组长退出后遗留的后代没有被清理")

    def test_fast_exit_still_records_mismatched_header(self):
        """组长在头部解析完成前退出：必须先处理完 stderr 再下终态结论。"""
        runtime = self.runtime()
        original = worker_runtime.HeaderScan.feed

        def slow_feed(scanner, line):
            time.sleep(0.08)
            return original(scanner, line)

        with patch.object(worker_runtime.HeaderScan, "feed", slow_feed), \
                patch.object(worker_runtime, "HEADER_DRAIN_GRACE_SECONDS", 0.01):
            state = self.spawn(runtime, model="vendor/header-model", effort="max",
                               env={"FAKE_CODEX_HEADER": "mismatch-model"})
            final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "failed",
                         "头部尚未解析完就宣布完成，会漏掉模型不一致")
        self.assertIn("不一致", final["error"])

    def test_stop_not_confirmed_keeps_running_with_error(self):
        """无法确认停止时不得伪造终态：保持 running 并给出明确错误。"""
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30"})
        pid = state["pid"]
        self.addCleanup(self._kill_pid, pid)
        with patch.object(worker_runtime, "_proc_table", return_value={}), \
                patch.object(runtime, "_stop_tree", return_value=False):
            result = runtime.cancel(state["agent_id"])
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["tree_stopped"])
        self.assertIsNotNone(result["stop_error"])
        self.assertIn("无法", result["stop_error"])
        self.assertIn("running", result["cancel_note"])
        self.assertIsNone(result["finished_at"])

    def _kill_pid(self, pid):
        if not process_alive(pid):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_unreadable_project_state_never_selects_global_default(self):
        """项目状态读取失败时不得回退到全局默认模型。"""
        config = self.config_path
        with patch.object(worker_runtime.project_rules, "status_project",
                          side_effect=OSError("synthetic read failure")):
            with self.assertRaises((worker_runtime.ContractError, worker_runtime.OperationError)) as caught:
                worker_runtime.resolve_selection(self.project, config_path=config)
        self.assertIn("拒绝静默改用全局默认值", str(caught.exception))
        self.assertNotIn("gpt-5.6-luna", str(caught.exception))
        with patch.object(worker_runtime.project_rules, "status_project",
                          side_effect=OSError("synthetic read failure")):
            with self.assertRaises((worker_runtime.ContractError, worker_runtime.OperationError)):
                runtime = worker_runtime.Runtime(codex_home=self.home,
                                                 command_prefix=[sys.executable, str(FAKE_CLI)])
                self.addCleanup(runtime.shutdown)
                runtime.spawn(task="x", cwd=str(self.project))

    def test_explicit_model_still_allowed_when_project_state_unreadable(self):
        """状态不可读不会静默换模型；显式给出完整组合仍然可用。"""
        with patch.object(worker_runtime.project_rules, "status_project",
                          side_effect=OSError("synthetic read failure")):
            selected = worker_runtime.resolve_selection(
                self.project, model="vendor/explicit-model", effort="max",
                config_path=self.config_path)
        self.assertEqual(selected["model"], "vendor/explicit-model")
        self.assertEqual(selected["reasoning_effort"], "max")
        self.assertEqual(selected["source"], "explicit")

    def test_log_privacy_keeps_secrets_out_of_results(self):
        runtime = self.runtime()
        secret = "SYNTHETIC_PRIVATE_TRACE_DO_NOT_RETURN"
        state = self.spawn(runtime, env={"FAKE_CODEX_EXIT": "3",
                                         "FAKE_CODEX_STDERR_EXTRA": "thinking\nfailed " + secret})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "failed")
        self.assertNotIn(secret, json.dumps(final))
        self.assertNotIn("thinking", final["error"])
        request = json.loads(Path(final["artifacts"]["request"]).read_text(encoding="utf-8"))
        self.assertNotIn(secret, json.dumps(request))

    def test_large_output_is_truncated_and_flagged(self):
        runtime = self.runtime()
        limit = worker_runtime.MAX_OUTPUT_BYTES
        state = self.spawn(runtime, env={"FAKE_CODEX_OUTPUT_BYTES": str(limit + 4096)})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["output_truncated"], "超出上限的输出必须显式标记截断")
        self.assertEqual(final["output_limit_bytes"], limit)
        self.assertLessEqual(len(final["output"].encode("utf-8")), limit)
        self.assertIn("只返回前", final["output_note"])
        self.assertIn("last-message.txt", final["output_note"])
        self.assertIn("只返回前", " ".join(final["warnings"]))

    def test_output_at_limit_is_not_flagged(self):
        runtime = self.runtime()
        state = self.spawn(runtime, env={"FAKE_CODEX_OUTPUT_BYTES": "4096"})
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["output_truncated"])
        self.assertIsNone(final["output_note"])


class PosixIdentityTests(RuntimeCase):
    """POSIX 进程身份回归：查不到进程表或无法比对身份时，绝不能当作已停止。"""

    def _live_agent(self, runtime):
        state = self.spawn(runtime, env={"FAKE_CODEX_SLEEP": "30"})
        self.addCleanup(self._reap, state["pid"])
        return runtime._agent(state["agent_id"])

    def _reap(self, pid):
        if not process_alive(pid):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_proc_table_unavailable_raises_instead_of_empty(self):
        if os.name != "posix":  # pragma: no cover - 仅在非 POSIX 上执行
            self.skipTest("POSIX 专用行为")
        with patch.object(worker_runtime, "_proc_table_from_proc", return_value=None), \
                patch.object(worker_runtime, "_proc_table_from_ps", return_value={}):
            with self.assertRaises(OSError) as caught:
                worker_runtime._proc_table()
        self.assertIn(worker_runtime.PROC_TABLE_UNAVAILABLE, str(caught.exception))
        with patch.object(worker_runtime, "_proc_table_from_proc", return_value=None), \
                patch.object(worker_runtime, "_proc_table_from_ps",
                             return_value={7: (1, 7, "S", "token")}):
            self.assertEqual(worker_runtime._proc_table(), {7: (1, 7, "S", "token")})

    def test_missing_start_token_never_matches(self):
        self.assertFalse(worker_runtime._same_process((1, 1, "S", None), "token-42"))
        self.assertFalse(worker_runtime._same_process((1, 1, "S", "token-42"), None))
        self.assertFalse(worker_runtime._same_process((1, 1, "S", "token-99"), "token-42"))
        self.assertTrue(worker_runtime._same_process((1, 1, "S", "token-42"), "token-42"))

    def test_unknown_identity_is_not_signalled(self):
        if os.name != "posix":  # pragma: no cover - 仅在非 POSIX 上执行
            self.skipTest("POSIX 专用行为")
        runtime = self.runtime()
        agent = self._live_agent(runtime)
        detached_pid = 999999
        other_pgid = (agent.pgid or 1) + 1000
        table = {detached_pid: (1, other_pgid, "S", None)}
        agent.descendants = {detached_pid: "token-42"}
        with patch.object(worker_runtime, "_proc_table", return_value=table), \
                patch.object(worker_runtime.os, "kill") as kill, \
                patch.object(worker_runtime.os, "killpg") as killpg:
            runtime._signal_tree(agent, signal.SIGTERM)
        self.assertNotIn(detached_pid, [call.args[0] for call in kill.call_args_list],
                         "无法比对身份的独立会话 PID 不能被信号终止")
        if agent.pgid:
            self.assertIn(agent.pgid, [call.args[0] for call in killpg.call_args_list])

    def test_unknown_identity_is_not_reported_as_stopped(self):
        if os.name != "posix":  # pragma: no cover - 仅在非 POSIX 上执行
            self.skipTest("POSIX 专用行为")
        runtime = self.runtime()
        agent = self._live_agent(runtime)
        known_pid, unknown_pid = 999997, 999999
        other_pgid = (agent.pgid or 1) + 1000
        matching = {known_pid: (1, other_pgid, "S", "token-42")}
        unknown = {unknown_pid: (1, other_pgid, "S", None)}
        with patch.object(worker_runtime, "_proc_table", return_value=matching):
            agent.descendants = {known_pid: "token-42"}
            _leader_exited, remaining = runtime._tree_report(agent)
        self.assertIn(known_pid, remaining, "启动令牌一致的存活后代必须仍然算作未停止")
        agent.descendants = {unknown_pid: "token-42"}
        with patch.object(worker_runtime, "_proc_table", return_value=unknown):
            with self.assertRaises(OSError) as caught:
                runtime._tree_report(agent)
        self.assertIn(worker_runtime.PROC_IDENTITY_UNKNOWN, str(caught.exception))
        with patch.object(worker_runtime, "_proc_table", return_value=unknown), \
                patch.object(worker_runtime.os, "kill"), \
                patch.object(worker_runtime.os, "killpg"):
            result = runtime.cancel(agent.agent_id)
        self.assertEqual(result["status"], "running", "无法确认身份时不得伪造成已停止")
        self.assertFalse(result["tree_stopped"])
        self.assertIn("无法", result["stop_error"])


class ArtifactTests(RuntimeCase):
    def test_job_dir_lives_under_project_tmp(self):
        runtime = self.runtime()
        state = self.spawn(runtime)
        job = self.job_dir(state)
        self.assertEqual(job.parent, self.project.resolve() / "tmp")
        self.assertTrue(job.name.startswith("delegate-worker-"))
        self.assertTrue(job.is_dir())
        self.wait_terminal(runtime, state["agent_id"])

    def test_symlinked_project_tmp_is_refused(self):
        project = self.make_git_project("symlink-project")
        target = self.root / "real-tmp"
        target.mkdir()
        try:
            (project / "tmp").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台差异
            self.skipTest(f"符号链接不可用：{exc}")
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.OperationError) as caught:
            runtime.spawn(task="x", cwd=str(project))
        self.assertIn("符号链接", str(caught.exception))
        self.assertEqual(runtime.list()["count"], 0)

    def test_invalid_cwd_and_sandbox_are_rejected(self):
        runtime = self.runtime()
        with self.assertRaises(worker_runtime.ContractError):
            runtime.spawn(task="x", cwd=str(self.root / "missing-dir"))
        with self.assertRaises(worker_runtime.ContractError):
            runtime.spawn(task="x", cwd=str(self.project), sandbox="danger-full-access")
        with self.assertRaises(worker_runtime.ContractError):
            runtime.spawn(task="", cwd=str(self.project))
        with self.assertRaises(worker_runtime.ContractError):
            runtime.spawn(task="x", cwd=str(self.project), writable_files=["ok", 7])
        self.assertEqual(runtime.list()["count"], 0)

    def test_missing_command_does_not_leave_live_children(self):
        runtime = worker_runtime.Runtime(codex_home=self.home,
                                         command_prefix=[str(self.root / "no-such-codex")])
        self.addCleanup(runtime.shutdown)
        with self.assertRaises(worker_runtime.OperationError) as caught:
            runtime.spawn(task="x", cwd=str(self.project))
        self.assertIn("无法启动", str(caught.exception))
        self.assertEqual(runtime.list()["count"], 0)


class ScopeTests(RuntimeCase):
    def test_nested_instruction_files_are_reported_not_parsed(self):
        project = self.make_git_project("nested")
        nested = project / "pkg"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("## 局部规则\n- 使用 pkg 的约定\n", encoding="utf-8")
        runtime = self.runtime()
        state = self.spawn(runtime, cwd=nested)
        paths = [item["path"] for item in state["nested_guidance"]]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(str(Path("pkg") / "AGENTS.md")), paths)
        self.assertEqual(state["nested_guidance"][0]["kind"], "potential-override")
        self.assertTrue(any("嵌套指令覆盖" in warning for warning in state["scope_warnings"]))
        self.assertTrue(all("未判断语义冲突" in warning or "嵌套" not in warning
                            for warning in state["scope_warnings"]))
        self.wait_terminal(runtime, state["agent_id"])

    def test_non_git_explicit_directory_still_works(self):
        plain = self.root / "plain-dir"
        plain.mkdir()
        runtime = self.runtime()
        state = self.spawn(runtime, cwd=plain)
        final = self.wait_terminal(runtime, state["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["resolved"]["model"], "gpt-5.6-luna")
        self.assertTrue(self.job_dir(final).is_dir())


if __name__ == "__main__":
    unittest.main()
