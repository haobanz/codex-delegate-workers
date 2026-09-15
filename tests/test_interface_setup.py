"""统一接口 MCP 注册的单元测试。

全部通过测试专用的假 Codex CLI 与独立的假 home 运行：不访问真实 ~/.codex，
不读写真实用户配置，也没有任何网络或模型调用。
"""

import importlib.util
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manage = _load("manage", SCRIPTS / "manage.py")
interface_setup = _load("interface_setup", SCRIPTS / "interface_setup.py")
sys.path.pop(0)


FAKE_SOURCE = '''#!/usr/bin/env python3
"""Test-only fake Codex CLI: edits a throwaway config.toml, no network or models.

FAKE_CODEX_FAIL=<moment>[,<moment>...]  moments: list, get, add, remove
FAKE_CODEX_ECHO=<text>                   write text to stderr before handling
FAKE_CODEX_LIST_BARE=1                   omit transport details from list output
FAKE_CODEX_ADD_CLOBBERS=1                overwrite the entry right after add
FAKE_CODEX_ADD_CLOBBERS=args             overwrite the command, keep args after add
FAKE_CODEX_ADD_DROPS=1                   delete the entry right after add
FAKE_CODEX_REMOVE_LEAVES=1               recreate the entry right after remove
"""

import json
import os
import sys


def config_path():
    return os.path.join(os.environ["CODEX_HOME"], "config.toml")


def read_text():
    try:
        with open(config_path(), "r", encoding="utf-8") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def read_servers():
    servers = {}
    current = None
    for line in read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[mcp_servers.") and stripped.endswith("]"):
            current = stripped[len("[mcp_servers."):-1]
            servers[current] = {}
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = None
            continue
        if current is None or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip()
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        if "." in key:
            continue
        servers[current][key] = parsed
    return servers


def entry(name):
    value = read_servers().get(name)
    if not isinstance(value, dict):
        return None
    return {"name": name, "enabled": value.get("enabled", True),
            "disabled_reason": None,
            "transport": {"type": "stdio", "command": value.get("command"),
                          "args": list(value.get("args", [])), "env": value.get("env"),
                          "env_vars": list(value.get("env_vars", [])), "cwd": value.get("cwd")},
            "enabled_tools": value.get("enabled_tools"),
            "disabled_tools": value.get("disabled_tools"),
            "startup_timeout_sec": value.get("startup_timeout_sec"),
            "tool_timeout_sec": value.get("tool_timeout_sec")}


def bare(name):
    value = entry(name)
    if value is not None:
        value.pop("transport", None)
    return value


def render(name, command, args, enabled=None, extra=""):
    lines = ["[mcp_servers.%s]" % name,
             "command = " + json.dumps(str(command)),
             "args = [" + ", ".join(json.dumps(item) for item in args) + "]"]
    if enabled is False:
        lines.append("enabled = false")
    if extra:
        lines.append(extra)
    return "\\n".join(lines) + "\\n"


def write_entry(name, command, args, drop_other=False):
    blocks = []
    current = None
    kept = []
    for line in read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[mcp_servers.") and stripped.endswith("]"):
            if current is not None:
                blocks.append(current)
            current = {"name": stripped[len("[mcp_servers."):-1], "lines": [line]}
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if current is not None:
                blocks.append(current)
                current = None
            kept.append(line)
            continue
        if current is not None:
            current["lines"].append(line)
        else:
            kept.append(line)
    if current is not None:
        blocks.append(current)
    output = [line for line in kept]
    for block in blocks:
        if block["name"] == name:
            continue
        output.extend(block["lines"])
    text = "\\n".join(line for line in output if line.strip())
    text = text.rstrip("\\n")
    if text:
        text += "\\n\\n"
    text += render(name, command, args)
    with open(config_path(), "w", encoding="utf-8") as stream:
        stream.write(text)


def remove_entry(name):
    blocks = []
    current = None
    kept = []
    removed = False
    for line in read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[mcp_servers.") and stripped.endswith("]"):
            if current is not None:
                blocks.append(current)
            current = {"name": stripped[len("[mcp_servers."):-1], "lines": [line]}
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if current is not None:
                blocks.append(current)
                current = None
            kept.append(line)
            continue
        if current is not None:
            current["lines"].append(line)
        else:
            kept.append(line)
    if current is not None:
        blocks.append(current)
    output = [line for line in kept]
    for block in blocks:
        if block["name"] == name:
            removed = True
            continue
        output.extend(block["lines"])
    with open(config_path(), "w", encoding="utf-8") as stream:
        stream.write("\\n".join(line for line in output if line.strip()).rstrip("\\n") + "\\n")
    return removed


def should_fail(moment):
    return moment in (os.environ.get("FAKE_CODEX_FAIL") or "").split(",")


def main(argv):
    echo = os.environ.get("FAKE_CODEX_ECHO")
    if echo:
        sys.stderr.write(echo + "\\n")
    if len(argv) < 3 or argv[1] != "mcp":
        sys.stderr.write("Error: unexpected command\\n")
        return 64
    action = argv[2]
    if action == "list":
        if should_fail("list"):
            sys.stderr.write("Error: failed to load configuration\\n")
            return 1
        if os.environ.get("FAKE_CODEX_LIST_BARE"):
            payload = [value for value in (bare(name) for name in read_servers()) if value]
        else:
            payload = [value for value in (entry(name) for name in read_servers()) if value]
        sys.stdout.write(json.dumps(payload) + "\\n")
        return 0
    if action == "get":
        if should_fail("get"):
            sys.stderr.write("Error: failed to load configuration\\n")
            return 1
        found = entry(argv[3])
        if found is None:
            sys.stderr.write("Error: No MCP server named '%s' found.\\n" % argv[3])
            return 1
        sys.stdout.write(json.dumps(found, indent=2) + "\\n")
        return 0
    if action == "add":
        if should_fail("add"):
            sys.stderr.write("Error: could not write configuration\\n")
            return 1
        name = argv[3]
        rest = argv[4:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        if not rest:
            sys.stderr.write("Error: a command is required\\n")
            return 2
        write_entry(name, rest[0], list(rest[1:]))
        if os.environ.get("FAKE_CODEX_ADD_CLOBBERS") == "args":
            write_entry(name, "/foreign/tool", list(rest[1:]))
        elif os.environ.get("FAKE_CODEX_ADD_CLOBBERS"):
            write_entry(name, "/foreign/tool", ["serve"])
        if os.environ.get("FAKE_CODEX_ADD_DROPS"):
            remove_entry(name)
        sys.stdout.write("Added global MCP server '%s'.\\n" % name)
        return 0
    if action == "remove":
        if should_fail("remove"):
            sys.stderr.write("Error: could not write configuration\\n")
            return 1
        name = argv[3]
        existed = remove_entry(name)
        if os.environ.get("FAKE_CODEX_REMOVE_LEAVES"):
            write_entry(name, "/foreign/tool", ["serve"])
        sys.stdout.write(("Removed global MCP server '%s'.\\n" if existed
                          else "No MCP server named '%s' found.\\n") % name)
        return 0
    sys.stderr.write("Error: unknown mcp subcommand\\n")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


FAKE_CODEX_STANDALONE = FAKE_SOURCE


def write_fake_cli(directory, name="fake-codex-cli.py"):
    """写出测试专用的假 Codex CLI，返回其绝对路径。"""
    path = Path(directory) / name
    path.write_text(FAKE_SOURCE, encoding="utf-8")
    return path


def parse_mcp_servers(config_bytes):
    """最小 TOML 读取：只解析 [mcp_servers.NAME] 段的字符串/数组/布尔值。"""
    servers, current = {}, None
    for line in config_bytes.decode("utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[mcp_servers.") and stripped.endswith("]"):
            current = stripped[len("[mcp_servers."):-1]
            servers[current] = {}
            continue
        if stripped.startswith("["):
            current = None
            continue
        if current is None or "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        try:
            servers[current][key] = json.loads(value)
        except ValueError:
            servers[current][key] = value
    return servers


class InterfaceTestCase(unittest.TestCase):
    """独立夹具：假 Codex CLI、假 home、假安装脚本，绝不使用真实用户配置。"""

    def setUp(self):
        self.temporary = temporary_directory(prefix="delegate-interface-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fake = write_fake_cli(self.root)
        self.home = self.root / "codex home"
        self.home.mkdir()
        self.main_bytes = (b'model = "chosen-by-user"\n'
                           b'model_reasoning_effort = "xhigh"\n\n'
                           b'[mcp_servers.other]\ncommand = "othercmd"\nargs = ["a", "b"]\n')
        self.config_path = self.home / "config.toml"
        self.config_path.write_bytes(self.main_bytes)
        self.skill = self.home / "skills/delegate-workers"
        (self.skill / "scripts").mkdir(parents=True)
        self.script = self.skill / "scripts/mcp_server.py"
        self.script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.secret = "SECRET-TOKEN-VALUE"
        self.interface = self.make_interface(self.home, self.skill)

    def make_interface(self, home, skill):
        return interface_setup.InterfaceSetup(
            home, skill, command_provider=lambda: [sys.executable, str(self.fake)])

    def set_environment(self, **values):
        """设置假 CLI 的行为开关，并在每个用例结束时恢复原环境。"""
        for key, value in values.items():
            previous = os.environ.get(key)
            self.addCleanup(self._restore_environment, key, previous)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _restore_environment(key, previous):
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    def after_cli_call(self, marker, action):
        """在假 Codex 收到某个子命令之后执行一次额外动作（模拟并发修改）。"""
        original = interface_setup.subprocess.run
        argv = marker.split()

        def wrapper(command, *arguments, **keywords):
            result = original(command, *arguments, **keywords)
            full = [*command, *arguments] if isinstance(command, list) else []
            if argv and any(run == argv for run in (full[index:index + len(argv)]
                                                    for index in range(len(full)))):
                action(full)
            return result

        return patch.object(interface_setup.subprocess, "run", side_effect=wrapper)

    def servers(self):
        if not self.config_path.exists():
            return {}
        return parse_mcp_servers(self.config_path.read_bytes())

    def marker_path(self):
        return self.home / interface_setup.MARKER_NAME

    def assert_environment_preserved(self):
        text = self.config_path.read_bytes()
        self.assertIn(b'model = "chosen-by-user"', text)
        self.assertIn(b'model_reasoning_effort = "xhigh"', text)
        self.assertEqual(self.servers().get("other"), {"command": "othercmd", "args": ["a", "b"]})

    def assert_not_registered(self):
        self.assertNotIn("delegate-workers", self.servers())
        self.assertFalse(self.marker_path().exists())


class AtomicWriteTests(InterfaceTestCase):
    def test_marker_staging_stays_in_project_tmp(self):
        scratch = self.root / "atomic-scratch"
        scratch.mkdir()
        destination = self.home / "marker-test.json"
        real_replace = interface_setup.os.replace

        def replace(source, target):
            self.assertTrue(Path(source).is_relative_to(scratch))
            self.assertEqual(Path(target), destination)
            return real_replace(source, target)

        with patch.object(interface_setup.platform_support, "project_tmp", return_value=scratch), \
                patch.object(interface_setup.os, "replace", side_effect=replace):
            interface_setup.atomic_write(destination, b"new marker")
        self.assertEqual(destination.read_bytes(), b"new marker")
        self.assertEqual(list(scratch.iterdir()), [])

    def test_replace_failure_preserves_destination_and_cleans_scratch(self):
        scratch = self.root / "atomic-scratch"
        scratch.mkdir()
        destination = self.home / "marker-test.json"
        destination.write_bytes(b"original marker")
        with patch.object(interface_setup.platform_support, "project_tmp", return_value=scratch), \
                patch.object(interface_setup.os, "replace", side_effect=OSError(18, "cross-device replace")):
            with self.assertRaises(OSError):
                interface_setup.atomic_write(destination, b"replacement marker")
        self.assertEqual(destination.read_bytes(), b"original marker")
        self.assertEqual(list(scratch.iterdir()), [])


class EnableTests(InterfaceTestCase):
    def test_enable_registers_exact_entry_and_minimal_marker(self):
        result = self.interface.enable()
        self.assertEqual(result["result"], "enabled")
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["interface"]["state"], "registered")
        entry = self.servers()["delegate-workers"]
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-X", "utf8", str(self.script), "--codex-home", str(self.home)])
        marker = json.loads(self.marker_path().read_text(encoding="utf-8"))
        self.assertEqual(set(marker) & {"model", "reasoning_effort", "parallel", "max_concurrency"},
                         set())
        self.assertEqual(marker["name"], "delegate-workers")
        self.assertEqual(marker["command"], sys.executable)
        self.assert_environment_preserved()

    def test_status_is_read_only_when_absent(self):
        before = self.config_path.read_bytes()
        status = self.interface.status()
        self.assertEqual(status["interface"]["state"], "absent")
        self.assertEqual(status["interface"]["state_label"], "未注册")
        self.assertFalse(status["restart_required"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.marker_path().exists())

    def test_repeated_enable_is_idempotent(self):
        self.assertEqual(self.interface.enable()["result"], "enabled")
        config_bytes = self.config_path.read_bytes()
        marker_bytes = self.marker_path().read_bytes()
        second = self.interface.enable()
        self.assertEqual(second["result"], "already_enabled")
        self.assertEqual(second["interface"]["state"], "registered")
        self.assertEqual(self.config_path.read_bytes(), config_bytes)
        self.assertEqual(self.marker_path().read_bytes(), marker_bytes)

    def test_enable_replaces_owned_disabled_entry(self):
        self.interface.enable()
        with self.config_path.open("a", encoding="utf-8") as stream:
            stream.write("enabled = false\n")
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "disabled")
        self.assertFalse(status["enabled"])
        result = self.interface.enable()
        self.assertEqual(result["result"], "enabled")
        self.assertEqual(result["interface"]["state"], "registered")

    def test_enable_refuses_foreign_same_name_entry(self):
        self.config_path.write_bytes(self.main_bytes + b'\n[mcp_servers.delegate-workers]\n'
                                                      b'command = "other-tool"\nargs = ["serve"]\n')
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "conflict")
        self.assertFalse(status["owned"])
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.enable()
        self.assertIn("不是本工具注册的内容", str(caught.exception))
        self.assertEqual(self.servers()["delegate-workers"]["command"], "other-tool")
        self.assertFalse(self.marker_path().exists())
        self.assert_environment_preserved()

    def test_enable_refuses_changed_owned_entry(self):
        self.interface.enable()
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text.replace("--codex-home", "--codex-home-renamed"),
                                    encoding="utf-8")
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "changed")
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.enable()
        self.assertIn("--codex-home-renamed", self.config_path.read_text(encoding="utf-8"))

    def test_enable_reports_failure_without_leaving_owned_entry(self):
        self.set_environment(FAKE_CODEX_FAIL="add")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.enable()
        self.assertIn("注册 MCP 条目失败", str(caught.exception))
        self.assert_not_registered()
        self.assert_environment_preserved()

    def test_post_write_clobber_by_foreign_config_is_not_deleted(self):
        self.set_environment(FAKE_CODEX_ADD_CLOBBERS="1")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.enable()
        self.assertIn("不属于本工具", str(caught.exception))
        self.assertIn("未删除它", str(caught.exception))
        self.assertEqual(self.servers()["delegate-workers"]["command"], "/foreign/tool")
        self.assertFalse(self.marker_path().exists())
        self.assert_environment_preserved()

    def test_post_write_disappearance_is_reported_and_marker_removed(self):
        self.set_environment(FAKE_CODEX_ADD_DROPS="1")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.enable()
        self.assertIn("复查", str(caught.exception))
        self.assert_not_registered()
        self.assert_environment_preserved()

    def test_post_write_disable_by_concurrent_change_is_not_claimed_enabled(self):
        """写入后条目被并发改为禁用：不谎报启用，也不删除该条目。"""
        def after_add(argv):
            with self.config_path.open("a", encoding="utf-8") as stream:
                stream.write("enabled = false\n")

        with self.after_cli_call("mcp add delegate-workers", after_add):
            with self.assertRaises(interface_setup.InterfaceError) as caught:
                self.interface.enable()
        self.assertIn("禁用状态", str(caught.exception))
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "disabled")
        self.assertFalse(status["enabled"])
        self.assertIn("delegate-workers", self.servers())

    def test_marker_from_other_install_does_not_claim_this_registration(self):
        """标记指向别的服务器路径时，不得把条目误报成本安装的注册。"""
        (self.home / interface_setup.MARKER_NAME).write_text(json.dumps({
            "schema_version": 1, "project": "delegate-workers", "name": "delegate-workers",
            "command": sys.executable, "args": ["-X", "utf8", "/elsewhere/mcp_server.py",
                                                "--codex-home", str(self.home)],
            "installed_script": "/elsewhere/mcp_server.py"}), encoding="utf-8")
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = " + json.dumps(sys.executable)
            + "\nargs = [\"-X\", \"utf8\", \"/elsewhere/mcp_server.py\", \"--codex-home\", "
            + json.dumps(str(self.home)) + "]\n").encode("utf-8"))
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "conflict")
        self.assertFalse(status["owned"])
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        self.assertIn("delegate-workers", self.servers())

    def test_marker_write_failure_leaves_configuration_unchanged(self):
        before = self.config_path.read_bytes()
        original = interface_setup.atomic_write

        def fail(path, data, *arguments, **keywords):
            if Path(path).name == interface_setup.MARKER_NAME:
                raise OSError("simulated marker write failure")
            return original(path, data, *arguments, **keywords)

        with patch.object(interface_setup, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                self.interface.enable()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.marker_path().exists())

    def _rewrite_entry(self, *, command=None, args=None, extra=""):
        """把已注册条目的命令或参数改成别的内容（模拟用户或他人修改）。"""
        text = self.config_path.read_text(encoding="utf-8")
        if command is not None:
            text = text.replace("command = " + json.dumps(sys.executable),
                                "command = " + json.dumps(command))
        if args is not None:
            current = json.dumps(["-X", "utf8", str(self.script), "--codex-home", str(self.home)])
            text = text.replace("args = " + current, "args = " + json.dumps(args))
        if extra:
            text = text.replace("[mcp_servers.delegate-workers]\n",
                                "[mcp_servers.delegate-workers]\n" + extra + "\n")
        self.config_path.write_text(text, encoding="utf-8")

    def test_command_only_edit_is_not_owned_and_is_preserved(self):
        """只改命令（参数仍是本安装的）不得再算所有权，且不得被删除。"""
        self.interface.enable()
        self._rewrite_entry(command="/foreign/tool")
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "changed")
        self.assertFalse(status["owned"])
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.enable()
        self.assertEqual(self.servers()["delegate-workers"]["command"], "/foreign/tool")
        self.assertEqual(self.servers()["delegate-workers"]["args"],
                         ["-X", "utf8", str(self.script), "--codex-home", str(self.home)])

    def test_command_only_edit_is_never_echoed_in_status(self):
        """被改过的命令可能带凭据，状态里不得出现原文本。"""
        self.interface.enable()
        self._rewrite_entry(command="runner --token " + self.secret)
        rendered = json.dumps(self.interface.status(), ensure_ascii=False)
        self.assertNotIn(self.secret, rendered)
        self.assertNotIn("runner", rendered)

    def test_foreign_entry_arguments_are_not_echoed_in_status(self):
        """同名他人条目的参数可能带凭据，状态里不得出现原文本。"""
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = \"other-tool\"\n"
            "args = [\"--api-key\", " + json.dumps(self.secret) + "]\n").encode("utf-8"))
        rendered = json.dumps(self.interface.status(), ensure_ascii=False)
        self.assertNotIn(self.secret, rendered)
        self.assertNotIn("other-tool", rendered)

    def test_cli_stderr_secret_is_not_returned_in_status_or_errors(self):
        """CLI 的任意输出都可能带凭据，状态与错误里只能保留退出码。"""
        self.set_environment(FAKE_CODEX_FAIL="list",
                             FAKE_CODEX_ECHO="failed with api_key=" + self.secret)
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "unavailable")
        self.assertIn("退出码 1", status["detail"])
        rendered = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(self.secret, rendered)
        self.assertNotIn("api_key", rendered)

    def test_previous_python_command_in_marker_stays_owned_and_is_preserved(self):
        """升级 Python 后仍使用旧解释器的注册保持所有权，重复启用不改写它。"""
        previous = "/usr/bin/python3.9"
        args = ["-X", "utf8", str(self.script), "--codex-home", str(self.home)]
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = " + json.dumps(previous)
            + "\nargs = " + json.dumps(args) + "\n").encode("utf-8"))
        self.marker_path().write_text(json.dumps({
            "schema_version": 1, "project": "delegate-workers", "name": "delegate-workers",
            "command": previous, "args": args, "installed_script": str(self.script)}),
            encoding="utf-8")
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "registered")
        self.assertTrue(status["owned"])
        self.assertEqual(status["entry"]["command"], previous)
        marker_bytes = self.marker_path().read_bytes()
        config_bytes = self.config_path.read_bytes()
        self.assertEqual(self.interface.enable()["result"], "already_enabled")
        self.assertEqual(self.marker_path().read_bytes(), marker_bytes)
        self.assertEqual(self.config_path.read_bytes(), config_bytes)
        self.assertEqual(json.loads(marker_bytes)["command"], previous)

    def test_marker_not_anchored_to_this_install_is_not_ownership_evidence(self):
        """标记把参数指向别的 home 时不得作为证据，条目按实际内容归类。"""
        args = ["-X", "utf8", str(self.script), "--codex-home", "/elsewhere/home"]
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = " + json.dumps(sys.executable)
            + "\nargs = " + json.dumps(args) + "\n").encode("utf-8"))
        self.marker_path().write_text(json.dumps({
            "schema_version": 1, "project": "delegate-workers", "name": "delegate-workers",
            "command": sys.executable, "args": args, "installed_script": str(self.script)}),
            encoding="utf-8")
        status = self.interface.status()["interface"]
        self.assertFalse(status["owned"])
        self.assertIn(status["state"], ("changed", "conflict"))
        self.assertIn("未锚定", status["marker_issue"] or "")

    def test_marker_command_that_is_not_python_is_not_ownership_evidence(self):
        """标记记录的不是 Python 解释器时不得授权所有权。"""
        args = ["-X", "utf8", str(self.script), "--codex-home", str(self.home)]
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = \"/foreign/tool\"\n"
            "args = " + json.dumps(args) + "\n").encode("utf-8"))
        self.marker_path().write_text(json.dumps({
            "schema_version": 1, "project": "delegate-workers", "name": "delegate-workers",
            "command": "/foreign/tool", "args": args, "installed_script": str(self.script)}),
            encoding="utf-8")
        status = self.interface.status()["interface"]
        self.assertFalse(status["owned"])
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        self.assertEqual(self.servers()["delegate-workers"]["command"], "/foreign/tool")

    def test_extra_modifiers_make_entry_changed_and_are_preserved(self):
        """条目被加了工具限制或超时选项时不再算所有权，也不会被删除。"""
        self.interface.enable()
        self._rewrite_entry(extra="startup_timeout_sec = 30")
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "changed")
        self.assertFalse(status["owned"])
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        self.assertIn("startup_timeout_sec", self.config_path.read_text(encoding="utf-8"))

    def test_extra_environment_makes_entry_changed_and_is_preserved(self):
        """条目带了环境变量时不再算所有权，且环境内容不会出现在状态里。"""
        self.interface.enable()
        self._rewrite_entry(extra='env = {"API_TOKEN": "%s"}' % self.secret)
        status = self.interface.status()["interface"]
        self.assertEqual(status["state"], "changed")
        self.assertFalse(status["owned"])
        rendered = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(self.secret, rendered)
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        self.assertIn("API_TOKEN", self.config_path.read_text(encoding="utf-8"))

    def test_post_write_command_change_is_not_deleted(self):
        """写入后命令被并发改成其他命令：保留对方内容，只报告未归属。"""
        self.set_environment(FAKE_CODEX_ADD_CLOBBERS="args")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.enable()
        self.assertIn("本工具记录的注册不同", str(caught.exception))
        self.assertEqual(self.servers()["delegate-workers"]["command"], "/foreign/tool")

    def test_status_server_present_tracks_actual_script(self):
        """server_present 必须反映本安装的服务器脚本是否真的存在。"""
        self.interface.enable()
        self.assertTrue(self.interface.status()["interface"]["server_present"])
        self.script.unlink()
        status = self.interface.status()["interface"]
        self.assertFalse(status["server_present"])
        self.assertIn("服务器脚本当前不存在", status["detail"])

    def test_status_without_codex_still_reports_server_present(self):
        """Codex CLI 不可用时 status 仍要如实报告服务器脚本是否存在。"""
        missing = interface_setup.InterfaceSetup(
            self.home, self.skill,
            command_provider=lambda: (_ for _ in ()).throw(ValueError("未找到可用的 Codex CLI")))
        status = missing.status()["interface"]
        self.assertEqual(status["state"], "unavailable")
        self.assertTrue(status["server_present"])

    def test_marker_is_only_rewritten_when_entry_actually_changes(self):
        """标记与条目一致时逐字节保留，避免无意义改写丢掉旧解释器记录。"""
        self.interface.enable()
        marker_bytes = self.marker_path().read_bytes()
        self.interface.status()
        self.interface.enable()
        self.assertEqual(self.marker_path().read_bytes(), marker_bytes)

    def test_reenabling_disabled_entry_keeps_previous_python_command(self):
        """重新启用被禁用的旧解释器条目时，不静默换成当前解释器。"""
        previous = "/usr/bin/python3.9"
        args = ["-X", "utf8", str(self.script), "--codex-home", str(self.home)]
        self.config_path.write_bytes(self.main_bytes + (
            "\n[mcp_servers.delegate-workers]\ncommand = " + json.dumps(previous)
            + "\nargs = " + json.dumps(args) + "\nenabled = false\n").encode("utf-8"))
        self.marker_path().write_text(json.dumps({
            "schema_version": 1, "project": "delegate-workers", "name": "delegate-workers",
            "command": previous, "args": args, "installed_script": str(self.script)}),
            encoding="utf-8")
        self.assertEqual(self.interface.status()["interface"]["state"], "disabled")
        self.assertEqual(self.interface.enable()["result"], "enabled")
        entry = self.servers()["delegate-workers"]
        self.assertEqual(entry["command"], previous)
        self.assertEqual(entry["args"], args)
        marker = json.loads(self.marker_path().read_text(encoding="utf-8"))
        self.assertEqual(marker["command"], previous)

    def test_timeout_reports_uncertain_state_instead_of_claiming_no_change(self):
        """超时可能已经写入配置，错误不得声称未做任何修改。"""
        def timeout(command, *arguments, **keywords):
            raise subprocess.TimeoutExpired(command, interface_setup.COMMAND_TIMEOUT)

        with patch.object(interface_setup.subprocess, "run", side_effect=timeout):
            with self.assertRaises(interface_setup.InterfaceError) as caught:
                self.interface.enable()
        message = str(caught.exception)
        self.assertIn("无法确认", message)
        self.assertNotIn("未做任何修改", message)

    def test_spaces_and_unicode_paths_round_trip(self):
        home = self.root / "codex home 用户目录"
        skill = self.root / "skills 技能目录/delegate-workers"
        (skill / "scripts").mkdir(parents=True)
        (skill / "scripts/mcp_server.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        home.mkdir()
        (home / "config.toml").write_bytes(self.main_bytes)
        interface = self.make_interface(home, skill)
        self.assertEqual(interface.enable()["interface"]["state"], "registered")
        args = json.loads(Path(home / "config.toml").read_text(encoding="utf-8").splitlines()[-1]
                          .split("=", 1)[1])
        self.assertEqual(args[2], str(skill / "scripts/mcp_server.py"))
        self.assertEqual(args[3:], ["--codex-home", str(home)])
        self.assertEqual(interface.status()["interface"]["state"], "registered")


class DisableTests(InterfaceTestCase):
    def test_disable_removes_owned_entry_and_marker(self):
        self.interface.enable()
        result = self.interface.disable()
        self.assertEqual(result["result"], "disabled")
        self.assertFalse(result["restart_required"])
        self.assertNotIn("delegate-workers", self.servers())
        self.assertEqual(self.interface.status()["interface"]["state"], "absent")
        self.assert_environment_preserved()

    def test_disable_without_entry_is_idempotent_and_creates_nothing(self):
        before = self.config_path.read_bytes()
        result = self.interface.disable()
        self.assertEqual(result["result"], "already_disabled")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.marker_path().exists())

    def test_disable_preserves_foreign_same_name_entry(self):
        self.config_path.write_bytes(self.main_bytes + b'\n[mcp_servers.delegate-workers]\n'
                                                      b'command = "other-tool"\nargs = ["serve"]\n')
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.disable()
        self.assertIn("不是本工具注册的内容", str(caught.exception))
        self.assertEqual(self.servers()["delegate-workers"]["command"], "other-tool")
        self.assert_environment_preserved()

    def test_disable_refuses_changed_entry(self):
        self.interface.enable()
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text.replace("--codex-home", "--codex-home-renamed"),
                                    encoding="utf-8")
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.disable()
        self.assertEqual(self.servers()["delegate-workers"]["args"][3], "--codex-home-renamed")

    def test_disable_failure_keeps_entry_and_marker(self):
        self.interface.enable()
        self.set_environment(FAKE_CODEX_FAIL="remove")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.disable()
        self.assertIn("停用统一接口失败", str(caught.exception))
        self.assertIn("delegate-workers", self.servers())
        self.assertTrue(self.marker_path().is_file())

    def test_disable_reports_entry_recreated_during_removal(self):
        self.interface.enable()
        self.set_environment(FAKE_CODEX_REMOVE_LEAVES="1")
        with self.assertRaises(interface_setup.InterfaceError) as caught:
            self.interface.disable()
        self.assertIn("复查仍存在同名条目", str(caught.exception))
        self.assertIn("delegate-workers", self.servers())
        self.assertTrue(self.marker_path().is_file())


class FailureTests(InterfaceTestCase):
    def test_list_failure_is_unavailable_not_absent(self):
        self.set_environment(FAKE_CODEX_FAIL="list")
        status = self.interface.status()
        self.assertEqual(status["interface"]["state"], "unavailable")
        self.assertIn("无法读取 Codex MCP 配置", status["interface"]["detail"])
        self.assertFalse(status["restart_required"])
        self.assertFalse(self.marker_path().exists())

    def test_enable_refuses_when_configuration_cannot_be_read(self):
        self.set_environment(FAKE_CODEX_FAIL="list")
        with self.assertRaises(interface_setup.InterfaceError):
            self.interface.enable()
        self.assertFalse(self.marker_path().exists())

    def test_get_failure_when_list_lacks_details_is_unavailable(self):
        self.config_path.write_bytes(self.main_bytes)
        self.interface.enable()
        self.set_environment(FAKE_CODEX_LIST_BARE="1", FAKE_CODEX_FAIL="get")
        status = self.interface.status()
        self.assertEqual(status["interface"]["state"], "unavailable")
        self.assertIn("delegate-workers", status["interface"]["detail"])

    def test_list_without_details_falls_back_to_get(self):
        self.interface.enable()
        self.set_environment(FAKE_CODEX_LIST_BARE="1")
        self.assertEqual(self.interface.status()["interface"]["state"], "registered")

    def test_missing_codex_cli_is_reported_without_touching_config(self):
        before = self.config_path.read_bytes()
        interface = interface_setup.InterfaceSetup(
            self.home, self.skill,
            command_provider=lambda: (_ for _ in ()).throw(ValueError("未找到可用的 Codex CLI 可执行文件")))
        status = interface.status()
        self.assertEqual(status["interface"]["state"], "unavailable")
        self.assertIn("未找到可用的 Codex CLI", status["interface"]["detail"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.marker_path().exists())

    def test_cli_json_that_is_not_json_is_reported(self):
        original = interface_setup.subprocess.run

        def bad_json(command, *arguments, **keywords):
            return subprocess.CompletedProcess(command, 0, "not json at all", "")

        with patch.object(interface_setup.subprocess, "run", side_effect=bad_json):
            status = self.interface.status()
        self.assertEqual(status["interface"]["state"], "unavailable")
        self.assertIn("不是有效 JSON", status["interface"]["detail"])

    def test_cli_timeout_is_reported(self):
        def timeout(command, *arguments, **keywords):
            raise subprocess.TimeoutExpired(command, interface_setup.COMMAND_TIMEOUT)

        with patch.object(interface_setup.subprocess, "run", side_effect=timeout):
            status = self.interface.status()
        self.assertEqual(status["interface"]["state"], "unavailable")
        self.assertIn("超时", status["interface"]["detail"])

    def test_cli_output_is_summarized_without_credentials_or_noise(self):
        self.set_environment(FAKE_CODEX_FAIL="list",
                             FAKE_CODEX_ECHO="Error: request failed with bearer_token=" + self.secret)
        status = self.interface.status()
        rendered = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(self.secret, rendered)
        self.assertLess(len(status["interface"]["detail"]), 260)

    def test_scratch_directory_uses_project_tmp_and_is_cleaned(self):
        project = self.root / "project"
        (project / ".git").mkdir(parents=True)
        seen = []
        original = interface_setup.subprocess.run

        def capture(command, *arguments, **keywords):
            environment = keywords.get("env") or {}
            seen.append((environment.get("CODEX_HOME"), environment.get("TMPDIR")))
            return original(command, *arguments, **keywords)

        with patch.object(interface_setup.subprocess, "run", side_effect=capture), \
                patch.object(Path, "cwd", return_value=project):
            self.interface.status()
        self.assertTrue(seen)
        for codex_home, scratch in seen:
            self.assertEqual(codex_home, str(self.home))
            self.assertTrue(scratch.startswith(str(project / "tmp")))
            self.assertFalse(scratch.startswith(("/tmp", "/var/tmp")))
        self.assertEqual(list((project / "tmp").iterdir()), [])

    def test_status_does_not_create_marker_or_change_config(self):
        before = self.config_path.read_bytes()
        self.interface.status()
        self.interface.status()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.marker_path().exists())


if __name__ == "__main__":
    unittest.main()
