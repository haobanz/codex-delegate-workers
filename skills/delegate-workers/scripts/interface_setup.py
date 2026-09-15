#!/usr/bin/env python3
"""显式的一次性 MCP 注册：只管理本工具自己的 delegate-workers 条目。

本模块通过本地 Codex CLI 的 ``codex mcp add|get|list|remove`` 维护配置里的
单个条目，不直接改写 config.toml、不改主代理模型或 workers.json，也不读取
或保存任何凭据。所有权只依据两类证据：

* home 下的最小标记文件 ``delegate-workers-interface.json``：只记录名称、命令、
  参数和安装脚本路径，不含模型、思考强度或密钥；标记必须锚定本安装的
  ``scripts/mcp_server.py`` 与 ``--codex-home <home>``，记录的命令须是 Python
  解释器（允许升级前的旧解释器路径）；
* 条目内容本身：stdio、命令与参数精确等于本安装的注册内容，且没有额外的
  env / env_vars / cwd 或工具、超时等选项。

同名条目只要不满足上述证据，就被当作他人配置，绝不覆盖或删除。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import platform_support


PROJECT = "delegate-workers"
SERVER_NAME = "delegate-workers"
MARKER_NAME = "delegate-workers-interface.json"
SCHEMA_VERSION = 1
COMMAND_TIMEOUT = 60
LAUNCHER_ARGS = ("-X", "utf8")

# 只会用到这些子命令：读取配置、注册或删除单个条目。login/logout、oauth 等
# 会触碰凭据的命令从不调用。
READ_COMMANDS = frozenset({"mcp list", "mcp get"})
MUTATING_COMMANDS = frozenset({"mcp add", "mcp remove"})

# 标记里记录的启动命令必须是 Python 解释器；升级前的旧解释器路径仍是有效证据。
_PYTHON_COMMAND = re.compile(r"(?i)^(python[0-9.]*[mu]?|py)(\.exe)?$")

# 条目上不允许出现的额外选项；改过这些选项的条目按“已修改”对待，绝不删除。
MODIFIER_KEYS = ("enabled_tools", "disabled_tools", "startup_timeout_sec", "tool_timeout_sec")

STATE_LABELS = {
    "registered": "已启用",
    "disabled": "条目存在但已禁用",
    "absent": "未注册",
    "changed": "内容与原始注册不一致，未改动",
    "conflict": "同名条目属于其他配置，未改动",
    "unavailable": "无法确认（本地 Codex CLI 或配置不可用）",
}


class InterfaceError(ValueError):
    """统一接口注册未满足安全前提，操作已停止。"""


class InterfaceUnavailable(InterfaceError):
    """本地 Codex CLI 或 MCP 配置当前不可读，状态无法确认。"""


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path, data, mode=0o600):
    """在项目 tmp 暂存并原子替换；跨文件系统失败时保留原目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="delegate-workers-interface-write-",
                                     dir=platform_support.project_tmp()) as scratch:
        descriptor, temporary = tempfile.mkstemp(prefix="marker-", dir=scratch)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _scratch_environment(home):
    """父环境副本 + 显式 CODEX_HOME；临时目录只放在调用项目的 tmp 下。"""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    try:
        scratch = tempfile.mkdtemp(prefix="delegate-workers-mcp-", dir=platform_support.project_tmp())
    except OSError as exc:
        raise InterfaceError(f"无法在项目 tmp 下创建临时目录，未运行 Codex 命令：{exc}") from exc
    env["TMPDIR"] = env["TMP"] = env["TEMP"] = scratch
    return env, scratch


def resolve_codex_command(provider=None):
    """返回不含 shell 的本地 Codex 命令前缀；失败时给出可读中文原因。"""
    if provider is not None:
        try:
            command = provider()
        except (ValueError, OSError) as exc:
            raise InterfaceUnavailable(f"无法定位本地 Codex CLI：{exc}") from exc
    else:
        try:
            import cli_support
        except ImportError as exc:
            raise InterfaceUnavailable(
                "当前安装缺少 scripts/cli_support.py，无法定位本地 Codex CLI；"
                "请先更新本工具再使用统一接口") from exc
        resolver = getattr(cli_support, "codex_command", None)
        if not callable(resolver):
            raise InterfaceUnavailable(
                "scripts/cli_support.py 缺少 codex_command()，无法定位本地 Codex CLI；请更新本工具")
        try:
            command = resolver()
        except (ValueError, OSError) as exc:
            raise InterfaceUnavailable(f"无法定位本地 Codex CLI：{exc}") from exc
    if (not isinstance(command, (list, tuple)) or not command
            or not all(isinstance(part, str) and part for part in command)):
        raise InterfaceUnavailable("本地 Codex 命令格式无效，未运行任何命令")
    return list(command)


def summarize_failure(result):
    """只回传退出码，不转述 CLI 的 stdout / stderr。

    CLI 输出可能夹带配置内容、凭据或隐藏推理，因此这里只保留退出码这一固定诊断，
    绝不返回任何原始文本片段。
    """
    code = getattr(result, "returncode", None)
    if not isinstance(code, int):
        return "退出码未知"
    return f"退出码 {code}"


def entry_fields(entry, *, owned):
    """窄化一条 MCP 条目。

    只有确认属于本安装的条目才回传 command / args；同名他人条目或内容被改过的
    条目可能夹带凭据，一律只报告形状信息，不返回任何文本。
    """
    if not isinstance(entry, dict):
        return None
    transport = entry.get("transport")
    transport = transport if isinstance(transport, dict) else {}
    fields = {
        "enabled": entry.get("enabled") if isinstance(entry.get("enabled"), bool) else None,
        "transport_type": transport.get("type"),
        "owned": bool(owned),
        "redacted": not owned,
    }
    if not owned:
        return fields
    args = transport.get("args")
    fields.update({
        "command": transport.get("command") if isinstance(transport.get("command"), str) else None,
        "args": list(args) if isinstance(args, list) and all(isinstance(item, str) for item in args)
                else None,
    })
    return fields


class InterfaceSetup:
    """维护单个 ``delegate-workers`` MCP 条目。"""

    def __init__(self, home, skill, command_provider=None):
        self.home = Path(home).expanduser().resolve()
        self.skill = Path(skill).expanduser().resolve()
        self._provider = command_provider

    @classmethod
    def for_installation(cls, installation, command_provider=None):
        return cls(installation.home, installation.skill, command_provider)

    def script(self):
        return self.skill / "scripts" / "mcp_server.py"

    def installed_script(self):
        return str(self.script())

    def launch_args(self, script=None):
        """本安装的启动参数；脚本路径与 home 都锚定到本次安装。"""
        target = self.installed_script() if script is None else str(script)
        return [*LAUNCHER_ARGS, target, "--codex-home", str(self.home)]

    @property
    def marker_path(self):
        return self.home / MARKER_NAME

    def marker_present(self):
        return self.marker_path.is_symlink() or self.marker_path.is_file()

    def server_present(self):
        """本安装的服务器脚本当前是否存在（状态展示与实际文件保持一致）。"""
        return self.script().is_file()

    def expected(self, command=None):
        """本工具拥有的注册内容；命令默认使用当前解释器。"""
        return {"command": command or sys.executable, "args": self.launch_args(), "env": {}}

    # ---------------------------------------------------------------- 标记文件

    @staticmethod
    def _python_like(command):
        """标记里记录的启动命令必须是 Python 解释器，不能是任意命令。"""
        if not isinstance(command, str) or not command:
            return False
        return bool(_PYTHON_COMMAND.match(re.split(r"[\\/]", command)[-1]))

    def read_marker(self):
        """返回 (标记或 None, 问题说明或 None)；只读，不创建任何文件。"""
        path = self.marker_path
        if path.is_symlink():
            return None, f"标记文件是符号链接，未采用：{path}"
        if not path.is_file():
            return None, None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, f"标记文件无法读取，未采用（{type(exc).__name__}）"
        if (not isinstance(value, dict) or value.get("project") != PROJECT
                or value.get("schema_version") != SCHEMA_VERSION or value.get("name") != SERVER_NAME
                or not isinstance(value.get("command"), str) or not value["command"]
                or not isinstance(value.get("args"), list) or not value["args"]
                or not all(isinstance(item, str) for item in value["args"])):
            return None, "标记文件格式无效，未采用"
        if value["args"] != self.launch_args() or not self._python_like(value["command"]):
            # 标记必须锚定本安装的服务器脚本与 home，且命令是 Python 解释器；
            # 否则它不能作为所有权证据，只按现有条目内容判断。
            return None, "标记文件未锚定本安装的服务器脚本与 home，未采用"
        if value.get("installed_script") not in (None, self.installed_script()):
            return None, "标记文件记录的服务器脚本与本安装不同，未采用"
        return value, None

    def write_marker(self, *, command=None, args=None):
        """记录本工具写入服务器的命令与参数；不含模型、强度或任何凭据。

        默认记录当前解释器；沿用旧解释器注册时传入实际写入的命令与参数，
        这样标记始终与配置里的条目一致。
        """
        expected = self.expected(command)
        value = {"schema_version": SCHEMA_VERSION, "project": PROJECT, "name": SERVER_NAME,
                 "command": command or expected["command"],
                 "args": list(args) if args is not None else expected["args"],
                 "installed_script": self.installed_script(), "updated_at": _timestamp()}
        atomic_write(self.marker_path, json_bytes(value))

    def marker_current(self, marker, command, args):
        """标记是否已经准确记录了给定的命令与参数。"""
        return (isinstance(marker, dict) and marker.get("command") == command
                and marker.get("args") == list(args))

    def track(self, entry):
        """让标记跟上当前拥有的条目；内容一致时不改写，避免丢掉旧解释器记录。

        只记录 Python 解释器启动的条目：标记是所有权证据，不能指向任意命令。
        """
        transport = entry.get("transport") if isinstance(entry, dict) else None
        if not isinstance(transport, dict):
            return
        command = transport.get("command")
        args = transport.get("args")
        if not self._python_like(command) or not isinstance(args, list):
            return
        if list(args) != self.launch_args():
            return
        marker, _ = self.read_marker()
        if self.marker_current(marker, command, list(args)):
            return
        self.write_marker(command=command, args=list(args))

    def remove_marker(self):
        path = self.marker_path
        if path.is_symlink() or path.is_file():
            path.unlink()

    def local_summary(self):
        """完全本地的摘要：不调用 Codex CLI，也不创建任何文件。"""
        marker, issue = self.read_marker()
        return {"name": SERVER_NAME, "server": self.installed_script(),
                "server_present": self.server_present(),
                "marker_path": str(self.marker_path), "marker_present": self.marker_present(),
                "marker_valid": marker is not None, "marker_issue": issue, "state": "unknown",
                "state_label": "本机摘要（未向 Codex 核对）", "entry": None,
                "detail": "运行菜单 7 或 interface status 可向本地 Codex CLI 核对实际注册状态"}

    # ------------------------------------------------------------------ CLI 调用

    def _run(self, command, arguments, env):
        try:
            return subprocess.run([*command, *arguments], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env,
                                  timeout=COMMAND_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as exc:
            raise InterfaceUnavailable("本地 Codex CLI 超时无响应；本次操作是否已写入配置无法确认") from exc
        except OSError as exc:
            raise InterfaceUnavailable(f"无法运行本地 Codex CLI：{exc}") from exc

    def _cli(self, command, arguments):
        """在调用项目 tmp 的临时环境下运行一次 Codex CLI。"""
        subtotal = " ".join(arguments[:2])
        if subtotal not in READ_COMMANDS | MUTATING_COMMANDS:
            raise InterfaceError(f"内部错误：不支持运行 codex {subtotal}，未执行任何命令")
        env, scratch = _scratch_environment(self.home)
        try:
            return self._run(command, arguments, env)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _list_entries(self, command):
        result = self._cli(command, ["mcp", "list", "--json"])
        if result.returncode:
            raise InterfaceUnavailable(f"无法读取 Codex MCP 配置（{summarize_failure(result)}）"
                                       "；CLI 输出未转述")
        try:
            entries = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise InterfaceUnavailable("Codex MCP 配置输出不是有效 JSON，无法确认注册状态") from exc
        if not isinstance(entries, list):
            raise InterfaceUnavailable("Codex MCP 配置输出格式无效，无法确认注册状态")
        return [entry for entry in entries if isinstance(entry, dict)]

    def _fetch_entry(self, command, entries):
        """先用 list 结果；只有它缺少条目细节时才单独 get 该项。"""
        raw = next((entry for entry in entries if entry.get("name") == SERVER_NAME), None)
        if raw is None or isinstance(raw.get("transport"), dict):
            return raw
        result = self._cli(command, ["mcp", "get", SERVER_NAME, "--json"])
        if result.returncode:
            raise InterfaceUnavailable(f"无法读取 MCP 条目 {SERVER_NAME}（{summarize_failure(result)}）"
                                       "；CLI 输出未转述")
        try:
            value = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise InterfaceUnavailable(f"MCP 条目 {SERVER_NAME} 的输出不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise InterfaceUnavailable(f"MCP 条目 {SERVER_NAME} 的输出格式无效")
        return value

    # ------------------------------------------------------------------ 状态判定

    @staticmethod
    def _enabled(entry):
        value = entry.get("enabled")
        return True if not isinstance(value, bool) else value

    @staticmethod
    def _plain(transport):
        """条目是否没有额外的 env / env_vars，并且没有固定 cwd。

        env 只接受空字典；字符串或列表形式的 env 视为被改过，不当作本工具的
        注册内容。
        """
        env, env_vars, cwd = (transport.get("env"), transport.get("env_vars"),
                              transport.get("cwd"))
        return (env is None or env == {}) and (env_vars is None or env_vars == []) \
            and (cwd is None or cwd == "")

    @staticmethod
    def _unmodified(entry):
        """条目是否没有本工具从不写入的工具、超时等额外选项。"""
        return all(entry.get(key) is None or entry.get(key) == [] or entry.get(key) is False
                   for key in MODIFIER_KEYS)

    def _stdlib_launch(self, transport):
        """命令与参数都精确等于本安装的注册内容。"""
        command = transport.get("command")
        if not isinstance(command, str) or not command:
            return False
        return transport.get("args") == self.launch_args()

    def _exact(self, entry, expected):
        """stdio + 命令与参数精确等于本安装的注册内容，且没有任何额外选项。

        只比较参数会让其他命令冒充本工具的注册，因此命令、参数、env /
        env_vars / cwd 以及工具与超时选项都必须一致。
        """
        transport = entry.get("transport")
        if not isinstance(transport, dict) or transport.get("type") != "stdio":
            return False
        if not self._stdlib_launch(transport) or not self._plain(transport):
            return False
        return self._unmodified(entry) and transport.get("command") == expected["command"]

    def _marker_match(self, entry, marker):
        """命令与参数等于本工具记录的内容；标记已由 read_marker 锚定本安装。

        允许条目仍使用标记里记录的旧解释器（例如升级 Python 之后），但命令
        必须逐字匹配，绝不接受被改成其他命令的条目。
        """
        if not marker:
            return False
        transport = entry.get("transport")
        if not isinstance(transport, dict) or transport.get("type") != "stdio":
            return False
        if not self._stdlib_launch(transport) or not self._plain(transport):
            return False
        if not self._unmodified(entry):
            return False
        return (transport.get("args") == marker["args"]
                and transport.get("command") == marker["command"])

    def _references_script(self, entry):
        """条目参数是否引用本安装的脚本（可能被用户改过其他字段）。"""
        transport = entry.get("transport")
        if not isinstance(transport, dict) or transport.get("type") != "stdio":
            return False
        args = transport.get("args")
        return isinstance(args, list) and self.installed_script() in args

    def classify(self, entry, marker, expected):
        """归类一条同名条目；owned 表示“本工具记录在案且内容未被改动的注册”。

        * registered / disabled：本工具拥有的注册，只是启用状态不同；
        * changed：指向本安装的脚本，但命令、参数或环境不等于本工具的注册内容
          （视为你的修改，保留现状）；
        * conflict：与本安装无关的同名条目，一律不碰。
        """
        marker_present = self.marker_present()
        if entry is None:
            return {"state": "absent", "owned": False, "references_install": False,
                    "enabled": None, "marker_present": marker_present,
                    "detail": "尚未注册统一接口条目"}
        enabled = self._enabled(entry)
        references = self._references_script(entry)
        if self._exact(entry, expected) or self._marker_match(entry, marker):
            state = "registered" if enabled else "disabled"
            detail = ("统一接口条目已启用并指向本安装" if enabled
                      else "统一接口条目存在但已禁用（Codex 不会启动它）")
            return {"state": state, "owned": True, "references_install": True,
                    "enabled": enabled, "marker_present": marker_present, "detail": detail}
        if references:
            return {"state": "changed", "owned": False, "references_install": True,
                    "enabled": enabled, "marker_present": marker_present,
                    "detail": "同名条目指向本安装的服务器，但命令、参数或环境与本工具记录的注册不同，"
                              "已保留现有内容（内容可能含凭据，未在此显示）"}
        return {"state": "conflict", "owned": False, "references_install": False,
                "enabled": enabled, "marker_present": marker_present,
                "detail": f"同名条目 {SERVER_NAME} 不是本工具注册的内容，未做任何改动"
                          "（内容可能含凭据，未在此显示）"}

    def _inspect(self):
        """读取一次实际注册；返回 (状态, 原始条目, Codex 命令前缀)。

        原始条目只供内部判定使用，绝不放进面向用户的输出：同名他人条目可能
        夹带凭据。
        """
        command = resolve_codex_command(self._provider)
        marker, marker_issue = self.read_marker()
        entry = self._fetch_entry(command, self._list_entries(command))
        result = self.classify(entry, marker, self.expected())
        present = self.server_present()
        result.update({"name": SERVER_NAME, "state_label": STATE_LABELS[result["state"]],
                       "marker_issue": marker_issue, "marker_path": str(self.marker_path),
                       "entry": entry_fields(entry, owned=result["owned"]),
                       "entry_redacted": not result["owned"], "expected": self.expected(),
                       "server": self.installed_script(), "server_present": present,
                       "home": str(self.home), "command": command})
        if marker_issue:
            result["detail"] = f"{result['detail']}；{marker_issue}"
        if not present and result["references_install"]:
            result["detail"] = (f"{result['detail']}；服务器脚本当前不存在：{self.installed_script()}"
                                "，该条目无法启动")
        return result, entry, command

    def _state(self):
        return self._inspect()[0]

    def state(self):
        """读取当前注册状态；CLI 或配置不可读时抛出可读错误。"""
        value = self._state()
        value["restart_required"] = value["state"] in {"registered", "disabled", "changed"}
        return value

    def status(self):
        """只读状态；CLI 不可用时报告 unavailable，不抛错、不写文件。"""
        value = self.describe()
        return {"project": "interface", "action": "status", "result": "status",
                "interface": value, "restart_required": value["restart_required"]}

    def describe(self):
        """只读诊断；任何失败都转成 unavailable，供状态展示使用。"""
        try:
            return self.state()
        except (InterfaceError, OSError, subprocess.SubprocessError) as exc:
            return {"name": SERVER_NAME, "state": "unavailable",
                    "state_label": STATE_LABELS["unavailable"], "owned": False, "enabled": None,
                    "marker_present": self.marker_present(), "marker_issue": None, "entry": None,
                    "entry_redacted": True,
                    "expected": self.expected(), "server": self.installed_script(),
                    "server_present": self.server_present(), "marker_path": str(self.marker_path),
                    "home": str(self.home), "restart_required": False,
                    "detail": str(exc) or type(exc).__name__}

    def _verify(self, command, expected):
        marker, _ = self.read_marker()
        entry = self._fetch_entry(command, self._list_entries(command))
        return entry, self.classify(entry, marker, expected)

    def _remove_entry(self, command):
        """删除单个条目并复查；返回 (是否已确认不存在, 说明或 None)。"""
        result = self._cli(command, ["mcp", "remove", SERVER_NAME])
        if result.returncode:
            return False, (f"删除条目失败（{summarize_failure(result)}）；CLI 输出未转述")
        if self._fetch_entry(command, self._list_entries(command)) is not None:
            return False, f"删除命令已执行，但复查仍存在同名条目"
        return True, None

    # ------------------------------------------------------------------ 变更操作

    def _preconditions(self):
        if not self.script().is_file():
            raise InterfaceError("当前安装缺少 scripts/mcp_server.py，无法注册统一接口；请先更新本工具")
        self.home.mkdir(parents=True, exist_ok=True)

    def _compensate(self, command, expected, cause):
        """写入后的状态不符合预期时，只回退本工具拥有的注册。

        内容被改过的条目（命令、参数或环境与记录不同）绝不删除：它可能带着
        用户自己的设置，甚至凭据。此时保留标记并如实报告，让用户自行核对。
        """
        try:
            entry, after = self._verify(command, expected)
        except InterfaceError as exc:
            raise InterfaceError(f"{cause}；复查也失败，无法确认最终状态：{exc}。请运行 "
                                 f"codex mcp get {SERVER_NAME} 复核") from exc
        if after["state"] == "registered":
            # 复查其实已经成功，不把可用状态回退掉。
            return
        if entry is None:
            if not self.marker_present():
                raise InterfaceError(f"{cause}；复查未发现本工具的条目")
            self.remove_marker()
            raise InterfaceError(f"{cause}；复查未发现本工具的条目")
        if not after["owned"]:
            # 同名条目不是本工具当前记录的注册：保留它的原样，只如实报告。
            if after["references_install"]:
                reason = "同名条目的命令、参数或环境与本工具记录的注册不同"
                # 内容被改过：保留标记，作为“这一条目由本安装创建过”的证据。
            else:
                reason = "同名条目不属于本工具"
                # 与本安装无关：本工具的注册没有成立，清理自己的标记。
                self.remove_marker()
            raise InterfaceError(f"{cause}；{reason}（{after['detail']}），"
                                 f"未删除它，请运行 codex mcp get {SERVER_NAME} 检查")
        if not after["enabled"]:
            # 已被并发改为禁用：它不会被启动，因此保留标记继续如实报告，不贸然删除。
            self.track(entry)
            raise InterfaceError(f"{cause}；同名条目当前为禁用状态，未改动它，请运行 "
                                 f"codex mcp get {SERVER_NAME} 检查")
        removed, problem = self._remove_entry(command)
        if not removed:
            raise InterfaceError(f"{cause}；回退也未完成：{problem}。请运行 "
                                 f"codex mcp get {SERVER_NAME} 复核后再重试")
        self.remove_marker()
        raise InterfaceError(f"{cause}；本次写入已回退，未留下启用中的条目")

    def enable(self):
        """幂等启用；只有本安装拥有的条目才会被写入。"""
        self._preconditions()
        command = resolve_codex_command(self._provider)
        expected = self.expected()
        current, entry, _ = self._inspect()
        state = current["state"]
        if not current["owned"] and state != "absent":
            raise InterfaceError(current["detail"] + "；为避免覆盖现有配置，未重新注册。"
                                 f"可先运行 codex mcp remove {SERVER_NAME} 清理后再启用")
        if state == "registered":
            # 已经由本工具启用：只补齐标记，不改动现有条目（可能仍用旧解释器）。
            self.track(entry)
            return {"project": "interface", "action": "enable", "result": "already_enabled",
                    "interface": self.describe(), "restart_required": True}
        # 重新写入时沿用条目自己记录的解释器，避免静默改掉你原先的启动命令。
        launch_command = expected["command"]
        if current["owned"] and isinstance(entry, dict):
            recorded = (entry.get("transport") or {}).get("command")
            if self._python_like(recorded):
                launch_command = recorded
        self.write_marker(command=launch_command, args=expected["args"])
        result = self._cli(command, ["mcp", "add", SERVER_NAME, "--",
                                     launch_command, *expected["args"]])
        if result.returncode:
            self._compensate(command, expected,
                             f"注册 MCP 条目失败（{summarize_failure(result)}）；CLI 输出未转述")
        _, after = self._verify(command, expected)
        if after["state"] != "registered":
            self._compensate(command, expected, "注册命令已执行，但复查未得到启用状态")
        return {"project": "interface", "action": "enable", "result": "enabled",
                "interface": self.describe(), "restart_required": True}

    def disable(self):
        """停用并移除本安装拥有的条目；同名他人条目保持原样。"""
        command = resolve_codex_command(self._provider)
        expected = self.expected()
        current, entry, _ = self._inspect()
        state = current["state"]
        if state == "absent":
            self.remove_marker()
            return {"project": "interface", "action": "disable", "result": "already_disabled",
                    "interface": self.describe(), "restart_required": False}
        if not current["owned"]:
            # 同名条目不属于本工具：保持原样，也绝不删除。
            raise InterfaceError(current["detail"] + "；如需删除请手工运行 "
                                 f"codex mcp remove {SERVER_NAME}")
        removed, problem = self._remove_entry(command)
        if not removed:
            raise InterfaceError("停用统一接口失败：" + problem
                                 + f"。请运行 codex mcp get {SERVER_NAME} 检查")
        self.remove_marker()
        return {"project": "interface", "action": "disable", "result": "disabled",
                "interface": self.describe(), "restart_required": False}

    # ------------------------------------------------------------ 生命周期钩子

    def guard_legacy_target(self, guidance):
        """应用到不含统一接口的版本前，拒绝留下仍会启动的失效条目。"""
        if not self.marker_present():
            return None
        try:
            current = self._state()
        except InterfaceError as exc:
            raise InterfaceError(
                f"无法确认统一接口条目状态，已停止操作：{exc}。请先运行 {guidance} 再重试") from exc
        if current["references_install"] and current["enabled"]:
            manual = (f"若该条目由你修改过，请手工运行 codex mcp remove {SERVER_NAME}"
                      if not current["owned"] else "")
            raise InterfaceError(
                f"统一接口条目 {SERVER_NAME} 已启用并指向本安装（{current['state_label']}），"
                f"而目标版本不含统一接口，操作后会留下失效的服务器路径。"
                f"请先运行 {guidance} 再重试" + (f"；{manual}" if manual else ""))
        return current

    def release_installation(self, guidance):
        """卸载前清理自己的条目；无法确认时拒绝继续，绝不留下失效条目。"""
        if not self.marker_present():
            return {"result": "not_registered"}
        try:
            command = resolve_codex_command(self._provider)
            current = self._state()
        except InterfaceError as exc:
            raise InterfaceError(
                f"无法确认统一接口条目已清理，已停止卸载：{exc}。请先运行 {guidance}，"
                f"或确认不再需要后删除标记文件 {self.marker_path} 再卸载") from exc
        if current["state"] == "absent":
            self.remove_marker()
            return {"result": "absent"}
        if not current["owned"]:
            if not current["references_install"]:
                # 同名条目与本安装无关：保留它，只清理本工具的标记。
                self.remove_marker()
                return {"result": "foreign_preserved", "detail": current["detail"]}
            if not current["enabled"]:
                # 已禁用的条目不会被启动；删除会影响你改过的内容，先如实报告。
                self.remove_marker()
                return {"result": "changed_disabled_preserved", "detail": current["detail"]}
            raise InterfaceError(
                f"统一接口条目 {SERVER_NAME} 已启用且指向本安装，但内容与本工具记录的注册不同；"
                f"卸载会留下失效的服务器路径，已停止卸载。请先运行 {guidance}；"
                f"若该条目由你修改过，请手工运行 codex mcp remove {SERVER_NAME} "
                f"确认删除后再卸载")
        removed, problem = self._remove_entry(command)
        if not removed:
            raise InterfaceError(f"无法删除本工具的 MCP 条目，已停止卸载：{problem}。"
                                 f"请先运行 {guidance} 或手工运行 codex mcp remove {SERVER_NAME} 再卸载")
        self.remove_marker()
        return {"result": "removed"}
