"""mcp_server 的协议与工具行为测试（通过真实 stdio 子进程，使用假 CLI）。"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from temp_support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/delegate-workers/scripts"
SERVER = SCRIPTS / "mcp_server.py"
FAKE_CLI = ROOT / "tests/fixtures/fake_codex.py"
TEMPLATE = ROOT / "skills/delegate-workers/references/project-delegation.md"
sys.path.insert(0, str(SCRIPTS))
import mcp_server  # noqa: E402
import project_rules  # noqa: E402
import worker_runtime  # noqa: E402
sys.path.pop(0)

PROTOCOL = "2025-06-18"
SNAPSHOT_WORKER = {"model": "deepseek/deepseek-v4.1-flash", "reasoning_effort": "max"}


def process_alive(pid):
    if pid is None:
        return False
    if os.name == "nt":  # pragma: no cover - 仅 Windows
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


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory(prefix="delegate-mcp-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        clean = project_rules._clean_git_environment
        boundary = patch.object(project_rules, "_clean_git_environment", side_effect=lambda: {
            **clean(), "GIT_CEILING_DIRECTORIES": str(self.root)})
        boundary.start()
        self.addCleanup(boundary.stop)
        self.home = self.root / "codex-home"
        self.write_workers({"default": {"model": "gpt-5.6-luna", "reasoning_effort": "medium"},
                            "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "high"}})
        self.project = self.make_git_project("project")
        self.clients = []

    def write_workers(self, profiles):
        path = self.home / "skills/delegate-workers/workers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "default_profile": "default",
                                    "profiles": profiles}, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def make_git_project(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def register(self, path, worker=SNAPSHOT_WORKER, profile="default"):
        return project_rules.init_project(path, template_path=TEMPLATE, profile=profile, worker=worker)

    # -- 真实子进程客户端 --------------------------------------------------

    def client(self, *, script=None, prefix=None, env=None, args=None):
        target = script or SERVER
        argv = [sys.executable, "-X", "utf8", str(target), "--codex-home", str(self.home)]
        argv.extend(args or [])
        if prefix is not None:
            # 测试包装器：把假 CLI 命令前缀注入服务器实例（生产路径没有这个开关）。
            wrapper = self.root / "wrapper.py"
            wrapper.write_text(
                "import sys, runpy\n"
                f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                f"import mcp_server\n"
                f"server = mcp_server.build_server(codex_home={str(self.home)!r},"
                f" command_prefix={list(prefix)!r})\n"
                "sys.exit(mcp_server.main_with(server, sys.argv[1:]))\n",
                encoding="utf-8")
            argv = [sys.executable, "-X", "utf8", str(wrapper), "--codex-home", str(self.home)]
        environment = dict(os.environ, PYTHONUNBUFFERED="1")
        environment.update(env or {})
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", env=environment, cwd=str(self.project))
        client = Client(self, process)
        self.clients.append(client)
        return client

    def tearDown(self):
        for client in self.clients:
            client.close()


class Client:
    """最小 stdio 客户端：写入原始行，读取一行原始响应。"""

    def __init__(self, case, process):
        self.case = case
        self.process = process
        self.next_id = 1

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def send_raw(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError("服务器意外关闭了 stdout")
        return json.loads(line)

    def call(self, method, params=None):
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.read()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def initialize(self):
        reply = self.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
        self.notify("notifications/initialized")
        return reply

    def tool(self, name, arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})

    def payload(self, name, arguments):
        reply = self.tool(name, arguments)
        self.case.assertNotIn("error", reply, reply)
        return reply["result"]["structuredContent"]

    def wait_terminal(self, agent_id, timeout=40.0):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.payload("get_agent", {"agent_id": agent_id})
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        self.case.fail(f"worker 未在 {timeout}s 内结束：{state and state['status']}")

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:  # pragma: no cover - 服务器可能已关闭管道
                pass
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.process.kill()
                self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):  # pragma: no cover - 已关闭或不可用
                pass


class LifecycleTests(ServerCase):
    def test_initialize_and_tools_list(self):
        client = self.client()
        reply = client.initialize()
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL)
        self.assertEqual(result["serverInfo"]["name"], "delegate-workers")
        self.assertEqual(result["capabilities"]["tools"]["listChanged"], False)
        self.assertIn("codex exec", result["instructions"])
        listed = client.call("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in listed],
                         ["list_models", "spawn_agent", "get_agent", "list_agents", "cancel_agent"])
        for tool in listed:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["description"])
        spawn_schema = next(tool for tool in listed if tool["name"] == "spawn_agent")
        self.assertEqual(spawn_schema["inputSchema"]["required"], ["task", "cwd"])
        self.assertEqual(spawn_schema["inputSchema"]["properties"]["sandbox"]["default"], "read-only")
        list_schema = next(tool for tool in listed if tool["name"] == "list_models")
        self.assertEqual(list_schema["inputSchema"]["required"], ["cwd"])

    def test_ping_and_initialized_notification_are_quiet(self):
        client = self.client()
        client.initialize()
        self.assertEqual(client.call("ping")["result"], {})
        client.notify("notifications/initialized")
        client.notify("notifications/cancelled", {"requestId": 1})
        payload = client.payload("list_agents", {})
        self.assertEqual(payload["agents"], [])

    def test_stdout_carries_only_protocol_messages(self):
        client = self.client()
        reply = client.initialize()
        self.assertEqual(reply["jsonrpc"], "2.0")
        self.assertEqual(reply["id"], 1)
        self.assertIn("result", reply)
        self.assertNotIn("serverInfo", json.dumps({"error": reply.get("error")}))

    def test_unknown_method_and_notification_unknown_method(self):
        client = self.client()
        client.initialize()
        reply = client.call("tools/nope")
        self.assertEqual(reply["error"]["code"], -32601)
        client.notify("tools/nope")
        self.assertEqual(client.call("ping")["result"], {})


class MalformedInputTests(ServerCase):
    def test_parse_error_does_not_stop_the_server(self):
        client = self.client()
        client.initialize()
        client.send_raw("{not json")
        reply = client.read()
        self.assertEqual(reply["id"], None)
        self.assertEqual(reply["error"]["code"], -32700)
        self.assertIn("Parse error", reply["error"]["message"])
        self.assertEqual(client.call("ping")["result"], {})

    def test_non_object_and_bad_jsonrpc_are_invalid_requests(self):
        client = self.client()
        client.initialize()
        for raw in ("[1,2,3]", '"text"', '42', 'null'):
            with self.subTest(raw=raw):
                client.send_raw(raw)
                reply = client.read()
                self.assertEqual(reply["error"]["code"], -32600)
                self.assertIsNone(reply["id"])
        client.send({"jsonrpc": "1.0", "id": 5, "method": "ping"})
        reply = client.read()
        self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(reply["id"], 5)

    def test_missing_or_non_string_method(self):
        client = self.client()
        client.initialize()
        client.send({"jsonrpc": "2.0", "id": 7})
        reply = client.read()
        self.assertEqual(reply["error"]["code"], -32600)
        client.send({"jsonrpc": "2.0", "id": 8, "method": 42})
        self.assertEqual(client.read()["error"]["code"], -32600)

    def test_params_must_be_objects(self):
        client = self.client()
        client.initialize()
        for params in ([1, 2], "text", 5):
            with self.subTest(params=params):
                reply = client.call("tools/call", params)
                self.assertEqual(reply["error"]["code"], -32602)
        for params in ([], "x", 3):
            with self.subTest(params=params):
                reply = client.call("tools/list", params)
                self.assertEqual(reply["error"]["code"], -32602)

    def test_invalid_effort_rejected_before_backend_lookup(self):
        # A backend with no dispatch method must never be touched by invalid input.
        server = mcp_server.Server(object())
        server.initialized = True
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "spawn_agent", "arguments": {
                                   "task": "x", "cwd": str(self.project),
                                   "reasoning_effort": "extreme"}}})
        self.assertEqual(reply["error"]["code"], -32602, reply)

    def test_tools_call_argument_validation(self):
        client = self.client()
        client.initialize()
        cwd = str(self.project)

        def expect(code, name, arguments, params=None):
            reply = client.call("tools/call", params or ({} if name is None else
                                                         {"name": name, "arguments": arguments}))
            self.assertEqual(reply["error"]["code"], code, reply)

        expect(-32602, "not_a_tool", {})
        expect(-32602, None, None, params={"arguments": {}})
        expect(-32602, "spawn_agent", None)
        expect(-32602, "spawn_agent", [])
        expect(-32602, "spawn_agent", {"task": "x"})
        expect(-32602, "spawn_agent", {"task": 5, "cwd": cwd})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": None})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "unknown": 1})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "model": "vendor/x"})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "sandbox": "danger-full-access"})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "writable_files": "not-a-list"})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "writable_files": ["a", 2]})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "reasoning_effort": "extreme"})
        expect(-32602, "spawn_agent", {"task": "x", "cwd": cwd, "profile": ""})
        expect(-32602, "list_models", {})
        expect(-32602, "list_models", {"cwd": "relative/path"})
        expect(-32602, "list_models", {"cwd": str(self.root / "missing")})
        expect(-32602, "list_models", {"cwd": cwd, "extra": True})
        expect(-32602, "list_agents", {"surprise": 1})
        expect(-32602, "get_agent", {})
        expect(-32602, "get_agent", {"agent_id": 5})
        expect(-32602, "cancel_agent", {})
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_notifications_never_mutate_state(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        # 未初始化/未声明 id 的通知即使携带工具参数也不得启动任何任务。
        for method in ("notifications/initialized", "tools/call", "spawn_agent", "ping"):
            client.notify(method, {"name": "spawn_agent",
                                   "arguments": {"task": "must not start", "cwd": str(self.project)}})
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_server_exposes_no_fake_backend_switch(self):
        for flag in ("--fake-cli", "--backend", "--mock", "--command-prefix", "--codex-path"):
            with self.subTest(flag=flag):
                client = self.client(args=[flag, "x"])
                try:
                    client.process.wait(timeout=20)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    client.process.kill()
                    self.fail(f"服务器接受了未知参数 {flag}")
                self.assertNotEqual(client.process.returncode, 0)

    def test_main_session_and_home_are_untouched(self):
        client = self.client()
        client.initialize()
        before = (self.home / "skills/delegate-workers/workers.json").read_bytes()
        self.assertEqual(client.payload("list_agents", {})["agents"], [])
        self.assertEqual((self.home / "skills/delegate-workers/workers.json").read_bytes(), before)
        self.assertFalse((self.home / "config.toml").exists())


class ListModelsTests(ServerCase):
    def test_default_project_snapshot_and_scope_notes(self):
        project = self.make_git_project("listed")
        self.register(project)
        client = self.client()
        client.initialize()
        payload = client.payload("list_models", {"cwd": str(project)})
        self.assertEqual(payload["default"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(payload["default"]["reasoning_effort"], "max")
        self.assertEqual(payload["default"]["source"], "project-snapshot")
        self.assertEqual(sorted(payload["profiles"]), ["complex", "default"])
        self.assertEqual(payload["profiles"]["default"]["compatibility"]["status"], "compatible")
        self.assertTrue(payload["accepts_custom_model"])
        self.assertFalse(payload["hardcoded_native_model_enum"])
        self.assertIn("不是供应商可用模型目录", payload["catalog_scope"])
        self.assertEqual(payload["sources"]["project_root"], str(project.resolve()))
        self.assertEqual(payload["sources"]["project_status"], "ok")

    def test_disabled_and_unregistered_use_global_default(self):
        disabled = self.make_git_project("disabled")
        self.register(disabled)
        project_rules.disable_project(disabled)
        client = self.client()
        client.initialize()
        for cwd in (str(disabled), str(self.project)):
            with self.subTest(cwd=cwd):
                payload = client.payload("list_models", {"cwd": cwd})
                self.assertEqual(payload["default"]["model"], "gpt-5.6-luna")
                self.assertEqual(payload["default"]["source"], "workers.json:default_profile")

    def test_corrupt_project_snapshot_is_reported_not_replaced(self):
        corrupt = self.make_git_project("corrupt")
        self.register(corrupt)
        (corrupt / project_rules.STATE_FILE).write_text("}{", encoding="utf-8")
        client = self.client()
        client.initialize()
        payload = client.payload("list_models", {"cwd": str(corrupt)})
        self.assertIsNone(payload["default"])
        self.assertIn("拒绝静默改用全局默认值", payload["default_error"])

    def test_nested_guidance_is_surfaced_as_scope_warning(self):
        project = self.make_git_project("nested")
        nested = project / "pkg"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("## 局部规则\n", encoding="utf-8")
        client = self.client()
        client.initialize()
        payload = client.payload("list_models", {"cwd": str(nested)})
        self.assertTrue(payload["nested_guidance"])
        self.assertTrue(any("嵌套指令" in warning for warning in payload["scope_warnings"]))


class PollingTests(ServerCase):
    def test_long_task_returns_immediately_and_is_not_killed_by_a_timer(self):
        """只要任务在运行就不该有运行时上限；较慢的任务仍应正常完成。"""
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_SLEEP": "3"})
        client.initialize()
        started = time.monotonic()
        payload = client.payload("spawn_agent", {"task": "slow but valid", "cwd": str(self.project)})
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(payload["status"], "running")
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertGreaterEqual(final["pid"] or 0, 0)

    def test_get_agent_is_idempotent_for_terminal_states(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "once", "cwd": str(self.project)})
        first = client.wait_terminal(payload["agent_id"])
        second = client.payload("get_agent", {"agent_id": payload["agent_id"]})
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["output"], second["output"])
        self.assertEqual(first["finished_at"], second["finished_at"])
        self.assertEqual(client.payload("list_agents", {})["count"], 1)


class ProtocolMetaTests(ServerCase):
    """MCP 保留字段 _meta、显式 null 与 cwd 绝对路径契约。"""

    def test_tools_call_accepts_validated_reserved_meta(self):
        client = self.client()
        client.initialize()
        for meta in ({"progressToken": "review"},
                     {"progressToken": 7},
                     {"progressToken": 7.5},
                     {},
                     {"vendor": {"nested": True}}):
            with self.subTest(meta=meta):
                reply = client.call("tools/call", {"name": "list_agents", "arguments": {},
                                                   "_meta": meta})
                self.assertNotIn("error", reply, reply)
                self.assertEqual(reply["result"]["structuredContent"]["agents"], [])

    def test_invalid_reserved_meta_is_rejected_before_dispatch(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        for meta in (["not", "an", "object"], "text", 5, {"progressToken": ["list"]},
                     {"progressToken": True}, {"progressToken": None}):
            with self.subTest(meta=meta):
                reply = client.call("tools/call", {"name": "list_agents", "arguments": {},
                                                   "_meta": meta})
                self.assertEqual(reply["error"]["code"], -32602, reply)
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_meta_on_initialize_ping_and_tools_list(self):
        client = self.client()
        reply = client.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"},
                                           "_meta": {"progressToken": "boot"}})
        self.assertNotIn("error", reply, reply)
        client.notify("notifications/initialized")
        self.assertEqual(client.call("ping", {"_meta": {"progressToken": 1}})["result"], {})
        listed = client.call("tools/list", {"cursor": None, "_meta": {"progressToken": "p"}})
        self.assertEqual(len(listed["result"]["tools"]), 5)
        bad = client.call("ping", {"_meta": "not-an-object"})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_tool_argument_keys_stay_strict_without_meta(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        reply = client.tool("spawn_agent", {"task": "x", "cwd": str(self.project),
                                            "_meta": {"progressToken": "smuggled"}})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_explicit_null_optional_arguments_are_rejected_without_dispatch(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        base = {"task": "never dispatch", "cwd": str(self.project)}
        for name in ("profile", "model", "reasoning_effort", "sandbox", "writable_files"):
            with self.subTest(argument=name):
                reply = client.tool("spawn_agent", {**base, name: None})
                self.assertEqual(reply["error"]["code"], -32602, reply)
        reply = client.tool("spawn_agent", {**base, "model": None,
                                            "reasoning_effort": "max"})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_absent_optional_arguments_still_use_defaults(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "use defaults",
                                                 "cwd": str(self.project)})
        self.assertEqual(payload["status"], "running")
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["sandbox"], "read-only")
        self.assertEqual(final["requested"]["writable_files"], [])

    def test_relative_and_missing_cwd_are_rejected_for_spawn_and_list(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        missing = str(self.root / "missing")
        for cwd in (".", "relative/path", "project", "../project", ""):
            with self.subTest(cwd=cwd):
                reply = client.tool("spawn_agent", {"task": "never dispatch", "cwd": cwd})
                self.assertEqual(reply["error"]["code"], -32602, reply)
                self.assertIn("cwd", reply["error"]["message"])
        for cwd in (".", "relative/path", missing):
            with self.subTest(cwd=cwd):
                reply = client.tool("list_models", {"cwd": cwd})
                self.assertEqual(reply["error"]["code"], -32602, reply)
        for cwd in (None, 5, [], {}):
            with self.subTest(cwd=cwd):
                reply = client.tool("spawn_agent", {"task": "never dispatch", "cwd": cwd})
                self.assertEqual(reply["error"]["code"], -32602, reply)
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_absolute_existing_cwd_is_accepted_and_resolved(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "absolute cwd",
                                                 "cwd": str(self.project / ".." / "project")})
        self.assertEqual(payload["cwd"], str(self.project.resolve()))


class SpawnTests(ServerCase):
    def spawn_args(self, **overrides):
        arguments = {"task": "run the fake worker", "cwd": str(self.project)}
        arguments.update(overrides)
        return arguments

    def test_spawn_returns_immediately_and_polls_to_completion(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args(model="vendor/mcp-model",
                                                                reasoning_effort="max"))
        self.assertEqual(payload["status"], "running")
        self.assertTrue(payload["agent_id"].startswith("wk-"))
        self.assertIsNone(payload["cli_session_id"])
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["resolved"]["model"], "vendor/mcp-model")
        self.assertEqual(final["resolved"]["reasoning_effort"], "max")
        self.assertEqual(final["resolved"]["source"], "explicit")
        self.assertIn("FAKE 最终回答", final["output"])
        self.assertTrue(Path(final["artifacts"]["result"]).is_file())

    def test_spawn_uses_project_snapshot_default(self):
        project = self.make_git_project("spawn-default")
        self.register(project)
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "use the project pair", "cwd": str(project)})
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["resolved"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(final["resolved"]["reasoning_effort"], "max")
        self.assertEqual(final["resolved"]["source"], "project-snapshot")

    def test_get_agent_unknown_id_is_tool_error(self):
        client = self.client()
        client.initialize()
        reply = client.tool("get_agent", {"agent_id": "wk-missing"})
        self.assertNotIn("error", reply)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("未知的 agent_id", reply["result"]["content"][0]["text"])

    def test_cancel_agent_unknown_id_is_tool_error(self):
        client = self.client()
        client.initialize()
        reply = client.tool("cancel_agent", {"agent_id": "wk-missing"})
        self.assertTrue(reply["result"]["isError"])

    def test_corrupt_project_snapshot_is_a_spawn_error_not_global_default(self):
        corrupt = self.make_git_project("spawn-corrupt")
        self.register(corrupt)
        (corrupt / project_rules.STATE_FILE).write_text("}{", encoding="utf-8")
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        reply = client.tool("spawn_agent", {"task": "x", "cwd": str(corrupt)})
        # 运行时刻意把不可确认的项目状态归为操作失败；无论走 -32602 还是工具 isError，
        # 都必须可诊断、且绝不静默改用全局默认值或真的派发。
        if "error" in reply:
            self.assertEqual(reply["error"]["code"], -32602)
            self.assertIn("拒绝静默改用全局默认值", reply["error"]["message"])
        else:
            self.assertTrue(reply["result"]["isError"], reply)
            self.assertIn("拒绝静默改用全局默认值", reply["result"]["content"][0]["text"])
        self.assertEqual(client.payload("list_agents", {})["count"], 0)

    def test_missing_cli_is_a_tool_error_when_not_injected(self):
        # 清空 PATH 与 HOME，确保找不到真实 Codex CLI；生产路径没有假后端开关。
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir(exist_ok=True)
        home = self.root / "empty-home"
        home.mkdir(exist_ok=True)
        client = self.client(env={"PATH": str(empty_bin), "HOME": str(home),
                                  "CODEX_HOME": str(self.home)})
        client.initialize()
        reply = client.tool("spawn_agent", self.spawn_args())
        self.assertNotIn("error", reply)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("Codex CLI", reply["result"]["content"][0]["text"])
        self.assertEqual(client.payload("list_agents", {})["count"], 0)
        self.assertEqual(sorted(path.name for path in (self.project / "tmp").iterdir())
                         if (self.project / "tmp").is_dir() else [], [])

    def test_worker_stdout_never_contaminates_protocol_stream(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_STDOUT_SPAM": "1"})
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args())
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["status"], "completed")
        # 服务器仍需正常应答：任何污染都会让下一次读取失败或不是合法响应。
        self.assertEqual(client.call("ping")["result"], {})

    def test_cancel_via_mcp_really_stops_process(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_SLEEP": "30"})
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args())
        self.assertTrue(process_alive(payload["pid"]))
        cancelled = client.payload("cancel_agent", {"agent_id": payload["agent_id"]})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["tree_stopped"])
        self.assertFalse(process_alive(payload["pid"]))
        listed = client.payload("list_agents", {})
        self.assertEqual(listed["agents"][0]["status"], "cancelled")

    def test_header_metadata_is_reported_separately_from_the_request(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "header check", "cwd": str(self.project),
                                                 "model": "vendor/mcp-model",
                                                 "reasoning_effort": "xhigh"})
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["resolved"]["model"], "vendor/mcp-model")
        self.assertEqual(final["resolved"]["reasoning_effort"], "xhigh")
        self.assertEqual(final["observed"]["status"], "recorded")
        self.assertEqual(final["observed"]["values"]["model"], "vendor/mcp-model")
        self.assertEqual(final["observed"]["values"]["reasoning effort"], "xhigh")
        self.assertTrue(final["cli_session_id"].startswith("fake-"))
        self.assertNotEqual(final["cli_session_id"], final["agent_id"])
        self.assertIn("不代表上游物理身份", final["observed"]["note"])

    def test_worker_error_message_never_contains_raw_stderr(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_EXIT": "7",
                                  "FAKE_CODEX_STDERR_EXTRA": "ERROR fake detail line"})
        client.initialize()
        payload = client.payload("spawn_agent", {"task": "fail now", "cwd": str(self.project)})
        final = client.wait_terminal(payload["agent_id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["exit_code"], 7)
        # stderr 可能含隐藏推理或凭据：只报告退出码与证据路径，不回传原始内容。
        self.assertIn("退出码 7", final["error"])
        self.assertNotIn("fake detail line", final["error"])
        self.assertNotIn("OpenAI Codex v0.0.0-fake", final["error"])
        self.assertNotIn("reasoning summaries", json.dumps(final))
        self.assertTrue(Path(final["artifacts"]["stderr_log"]).is_file())

    def test_late_cancel_keeps_completed_result(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args())
        done = client.wait_terminal(payload["agent_id"])
        self.assertEqual(done["status"], "completed")
        late = client.payload("cancel_agent", {"agent_id": payload["agent_id"]})
        self.assertEqual(late["status"], "completed")
        self.assertIn("已处于终态", late["cancel_note"])


class ShutdownTests(ServerCase):
    def test_stdin_eof_terminates_owned_children(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_SLEEP": "30"})
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args())
        pid = payload["pid"]
        self.assertTrue(process_alive(pid))
        client.process.stdin.close()
        client.process.wait(timeout=40)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), "stdin EOF 后 worker 子进程仍在运行")

    def test_sigterm_terminates_owned_children(self):
        if os.name != "posix":  # pragma: no cover - 仅 POSIX 有 SIGTERM
            self.skipTest("该用例只在 POSIX 上有意义")
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)],
                             env={"FAKE_CODEX_SLEEP": "30"})
        client.initialize()
        payload = client.payload("spawn_agent", self.spawn_args())
        pid = payload["pid"]
        self.assertTrue(process_alive(pid))
        client.process.terminate()
        try:
            client.process.wait(timeout=40)
        except subprocess.TimeoutExpired:  # pragma: no cover
            client.process.kill()
            self.fail("服务器未响应 SIGTERM")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), "SIGTERM 后 worker 子进程仍在运行")

    def test_broken_pipe_on_stdout_is_tolerated(self):
        client = self.client(prefix=[sys.executable, str(FAKE_CLI)])
        client.initialize()
        client.process.stdout.close()
        client.process.stdin.close()
        self.assertEqual(client.process.wait(timeout=40), 0)

    def spawn_args(self):
        return {"task": "long job", "cwd": str(self.project)}


if __name__ == "__main__":
    unittest.main()
