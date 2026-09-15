#!/usr/bin/env python3
"""delegate-workers 统一 MCP 服务器（stdio，JSON-RPC 2.0，无第三方依赖）。

在本工具自己的命名空间下暴露 list_models、spawn_agent、get_agent、list_agents、
cancel_agent 五个工具：每次派发都会启动一个独立的本地 ``codex exec`` 进程，沿用
当前 CODEX_HOME 中已有的供应商与认证。这里不会替换宿主内建的原生派发工具，也
不声称任何上游物理模型身份。

stdout 只输出协议消息；人类可读日志一律写到 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cli_support  # noqa: E402
import platform_support  # noqa: E402
import worker_runtime  # noqa: E402


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "delegate-workers"
DEFAULT_SERVER_VERSION = "0.0.0"
EFFORT_VALUES = worker_runtime.EFFORT_VALUES
SANDBOX_VALUES = list(worker_runtime.SANDBOXES)
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def server_version():
    try:
        value = (HERE.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SERVER_VERSION
    return value or DEFAULT_SERVER_VERSION


def _error(code, message, *, data=None):
    payload = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return payload


class ProtocolError(Exception):
    """JSON-RPC 协议层错误（未知方法、参数不合法等）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 参数校验工具
# ---------------------------------------------------------------------------


def _require_object(value, label):
    if not isinstance(value, dict):
        raise ProtocolError(INVALID_PARAMS, f"{label} 必须是 JSON 对象")
    return value


def _validate_meta(params, label="params"):
    """校验 MCP 协议保留的 ``params._meta``（合法时允许，非法时报 -32602）。"""
    if not isinstance(params, dict) or "_meta" not in params:
        return None
    meta = params["_meta"]
    if not isinstance(meta, dict):
        raise ProtocolError(INVALID_PARAMS, f"{label} 的 _meta 必须是 JSON 对象")
    if "progressToken" in meta:
        token = meta["progressToken"]
        if isinstance(token, bool) or not isinstance(token, (str, int, float)):
            raise ProtocolError(INVALID_PARAMS,
                                f"{label} 的 _meta.progressToken 必须是字符串或数字")
    return meta


def _checked_keys(arguments, required, optional):
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ProtocolError(INVALID_PARAMS, "缺少必需参数：" + "、".join(missing))
    unknown = [name for name in arguments if name not in required and name not in optional]
    if unknown:
        raise ProtocolError(INVALID_PARAMS, "包含未知参数：" + "、".join(sorted(unknown)))


def _absolute_directory(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(INVALID_PARAMS, f"{label} 必须是非空字符串")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProtocolError(INVALID_PARAMS, f"{label} 必须是绝对路径：{value}")
    if not path.is_dir():
        raise ProtocolError(INVALID_PARAMS, f"{label} 不是已存在的目录：{value}")
    return str(path.resolve())


def _optional_string(arguments, name, label):
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(INVALID_PARAMS, f"{label} 必须是非空字符串")
    return value


def _optional_choice(arguments, name, choices, label):
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, str) or value not in choices:
        raise ProtocolError(INVALID_PARAMS, f"{label} 取值无效：{value!r}；可选：{', '.join(choices)}")
    return value


def _string_list(arguments, name, label):
    if name not in arguments:
        return []
    value = arguments[name]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProtocolError(INVALID_PARAMS, f"{label} 必须是字符串数组")
    return list(value)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


def tool_definitions():
    return [
        {
            "name": "list_models",
            "title": "列出已配置的执行模型",
            "description": "列出本工具已安装 workers.json 中的模型预设、当前生效的默认选择（启用且完整的项目快照优先，"
                           "否则用全局默认预设）以及静态兼容性预检结果。这不是供应商可用模型目录，"
                           "也不代表运行时探测或上游身份验证；自定义模型 ID 可以原样传给 spawn_agent。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string",
                            "description": "绝对路径的现有目录，用于发现该项目自己的委派快照。"},
                },
                "required": ["cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "spawn_agent",
            "title": "启动一个执行 worker",
            "description": "立即返回并启动一个独立的本地 codex exec 进程（全新进程，不复用旧会话），显式传入模型与思考强度，"
                           "沿用当前 CODEX_HOME 的供应商和认证。任务较长时请稍后用 get_agent 轮询；"
                           "返回的 agent_id 是本适配器生成的 ID，不是 CLI 会话 ID。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "交给 worker 的完整任务说明（必填）。"},
                    "cwd": {"type": "string",
"description": "worker 的工作目录，必须是已存在的绝对路径（必填）；相对路径会被拒绝。"},
                    "profile": {"type": "string",
                                "description": "已安装 workers.json 中的预设名称；不传则用项目快照或全局默认。"},
                    "model": {"type": "string",
                              "description": "显式模型 ID（原样传给 CLI，可为自定义供应商模型）；必须同时给出 reasoning_effort。"},
                    "reasoning_effort": {"type": "string", "enum": EFFORT_VALUES,
                                         "description": "思考强度；只给强度时会覆盖所选预设的强度。"},
                    "sandbox": {"type": "string", "enum": SANDBOX_VALUES, "default": "read-only",
                                "description": "worker 的沙箱模式，默认 read-only。"},
                    "writable_files": {"type": "array", "items": {"type": "string"},
                                       "description": "写给 worker 的可写范围说明（指令边界，不是文件系统 ACL）。"},
                },
                "required": ["task", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_agent",
            "title": "查询 worker 状态",
            "description": "按 agent_id 查询一次任务状态：running / completed / failed / cancelled，"
                           "以及请求参数、CLI 启动头部记录的配置、最终输出、错误、退出码和证据文件路径。"
                           "缺失的启动元数据按未验证处理，不等于失败。",
            "inputSchema": {
                "type": "object",
                "properties": {"agent_id": {"type": "string", "description": "spawn_agent 返回的 agent_id。"}},
                "required": ["agent_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_agents",
            "title": "列出全部 worker",
            "description": "列出本次 MCP 服务器进程启动过的所有 worker 及其最新状态；不接收任何参数。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "cancel_agent",
            "title": "取消 worker",
            "description": "终止该 worker 的进程树（POSIX 进程组 / Windows taskkill /T），确认停止后才返回 cancelled；"
                           "如果任务已经完成或失败，则保留其真实结果，不会改写状态。",
            "inputSchema": {
                "type": "object",
                "properties": {"agent_id": {"type": "string", "description": "spawn_agent 返回的 agent_id。"}},
                "required": ["agent_id"],
                "additionalProperties": False,
            },
        },
    ]


def _tool_result(payload, *, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


def _operation_error(exc):
    return _tool_result({"error": str(exc), "isError": True}, is_error=True)


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------


class Server:
    """一个 MCP 服务器实例；每个连接使用一个 Runtime 拥有它的子进程。"""

    def __init__(self, runtime, *, stdout=None, stderr=None):
        self.runtime = runtime
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.tools = {tool["name"]: tool for tool in tool_definitions()}
        self._write_lock = threading.Lock()
        self.initialized = False

    # -- 协议层 -----------------------------------------------------------

    def handle(self, message):
        """返回要写出的响应对象；通知或客户端响应返回 None。"""
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": _error(INVALID_REQUEST, "Invalid Request：JSON-RPC 消息必须是对象")}
        has_id = "id" in message
        identifier = message.get("id")
        if has_id and not (identifier is None or isinstance(identifier, (str, int, float))
                           and not isinstance(identifier, bool)):
            return {"jsonrpc": "2.0", "id": None,
                    "error": _error(INVALID_REQUEST, "Invalid Request：id 必须是字符串、数字或 null")}
        if message.get("jsonrpc") != "2.0":
            return ({"jsonrpc": "2.0", "id": identifier,
                     "error": _error(INVALID_REQUEST, "Invalid Request：jsonrpc 必须为 \"2.0\"")}
                    if has_id else None)
        method = message.get("method")
        if method is None:
            if "result" in message or "error" in message:
                return None  # 客户端对我们请求的响应；本服务器不发送请求，直接忽略
            if not has_id:
                return None
            return {"jsonrpc": "2.0", "id": identifier,
                    "error": _error(INVALID_REQUEST, "Invalid Request：缺少 method")}
        if not isinstance(method, str):
            return ({"jsonrpc": "2.0", "id": identifier,
                     "error": _error(INVALID_REQUEST, "Invalid Request：method 必须是字符串")}
                    if has_id else None)
        if not has_id:
            return None  # 通知一律不产生响应，也不执行任何变更
        try:
            _validate_meta(message.get("params"))
            result = self.dispatch(method, message.get("params"))
        except ProtocolError as exc:
            return {"jsonrpc": "2.0", "id": identifier,
                    "error": _error(exc.code, exc.message)}
        except Exception as exc:  # noqa: BLE001 - 保持服务器存活
            self.log(f"内部错误：{exc!r}")
            return {"jsonrpc": "2.0", "id": identifier,
                    "error": _error(INTERNAL_ERROR, f"服务器内部错误：{exc}")}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def dispatch(self, method, params):
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            if params is not None and not isinstance(params, dict):
                raise ProtocolError(INVALID_PARAMS, "ping 的 params 必须是 JSON 对象")
            return {}
        if method == "tools/list":
            if params is not None and not isinstance(params, dict):
                raise ProtocolError(INVALID_PARAMS, "tools/list 的 params 必须是 JSON 对象")
            if isinstance(params, dict) and params.get("cursor") is not None:
                cursor = params["cursor"]
                if not isinstance(cursor, str) or not cursor.strip():
                    raise ProtocolError(INVALID_PARAMS, "tools/list 的 cursor 必须是非空字符串")
            return {"tools": [self.tools[name] for name in self.tools]}
        if method == "tools/call":
            return self.tools_call(params)
        raise ProtocolError(METHOD_NOT_FOUND, f"Method not found：{method}")

    def initialize(self, params):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProtocolError(INVALID_PARAMS, "initialize 的 params 必须是 JSON 对象")
        requested = params.get("protocolVersion")
        if requested is not None and (not isinstance(requested, str) or not requested.strip()):
            raise ProtocolError(INVALID_PARAMS, "initialize 的 protocolVersion 必须是非空字符串")
        capabilities = params.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise ProtocolError(INVALID_PARAMS, "initialize 的 capabilities 必须是 JSON 对象")
        client = params.get("clientInfo")
        if client is not None and not isinstance(client, dict):
            raise ProtocolError(INVALID_PARAMS, "initialize 的 clientInfo 必须是 JSON 对象")
        if isinstance(client, dict):
            for field in ("name", "version"):
                value = client.get(field)
                if value is not None and not isinstance(value, str):
                    raise ProtocolError(INVALID_PARAMS,
                                        f"initialize 的 clientInfo.{field} 必须是字符串")
        self.initialized = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": server_version()},
            "instructions": (
                "delegate-workers 统一接口：用 spawn_agent 启动一个独立的本地 codex exec 进程"
                "（沿用当前 CODEX_HOME 的供应商与认证，显式传入模型与思考强度），"
                "任务较长时先返回、稍后用 get_agent 轮询，用 list_agents 查看全部，用 cancel_agent 终止进程树。"
                "工具返回的启动头部配置只代表本地记录的启动参数，不代表上游物理模型身份；"
                "显式指定 model 时必须同时给出 reasoning_effort。"
            ),
        }

    def tools_call(self, params):
        params = _require_object(params, "tools/call 的 params")
        _validate_meta(params, "tools/call 的 params")
        _checked_keys(params, ["name"], ["arguments", "_meta"])
        name = params["name"]
        if not isinstance(name, str) or name not in self.tools:
            raise ProtocolError(INVALID_PARAMS, f"未知工具：{name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(INVALID_PARAMS, "tools/call 的 arguments 必须是 JSON 对象")
        handler = getattr(self, "tool_" + name)
        return handler(arguments)

    # -- 工具实现 ---------------------------------------------------------

    def tool_list_models(self, arguments):
        _checked_keys(arguments, ["cwd"], [])
        cwd = _absolute_directory(arguments["cwd"], "cwd")
        try:
            payload = self.runtime.list_models(cwd)
        except (worker_runtime.OperationError, worker_runtime.ContractError) as exc:
            return _operation_error(exc)
        return _tool_result(payload)

    def tool_spawn_agent(self, arguments):
        _checked_keys(arguments, ["task", "cwd"],
                      ["profile", "model", "reasoning_effort", "sandbox", "writable_files"])
        task = arguments["task"]
        if not isinstance(task, str) or not task.strip():
            raise ProtocolError(INVALID_PARAMS, "task 必须是非空字符串")
        cwd = _absolute_directory(arguments["cwd"], "cwd")
        profile = _optional_string(arguments, "profile", "profile")
        model = _optional_string(arguments, "model", "model")
        effort = _optional_choice(arguments, "reasoning_effort", EFFORT_VALUES, "reasoning_effort")
        if model is not None and effort is None:
            raise ProtocolError(INVALID_PARAMS,
                                "显式指定 model 时必须同时显式指定 reasoning_effort")
        sandbox = _optional_choice(arguments, "sandbox", SANDBOX_VALUES, "sandbox")
        if sandbox is None:
            sandbox = worker_runtime.SANDBOX_DEFAULT
        writable_files = _string_list(arguments, "writable_files", "writable_files")
        try:
            payload = self.runtime.spawn(task=task, cwd=cwd, profile=profile, model=model,
                                         effort=effort, sandbox=sandbox,
                                         writable_files=writable_files)
        except worker_runtime.ContractError as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc)) from exc
        except worker_runtime.OperationError as exc:
            return _operation_error(exc)
        return _tool_result(payload)

    def tool_get_agent(self, arguments):
        _checked_keys(arguments, ["agent_id"], [])
        return self._agent_result(arguments["agent_id"], self.runtime.get)

    def tool_list_agents(self, arguments):
        _checked_keys(arguments, [], [])
        return _tool_result(self.runtime.list())

    def tool_cancel_agent(self, arguments):
        _checked_keys(arguments, ["agent_id"], [])
        return self._agent_result(arguments["agent_id"], self.runtime.cancel)

    def _agent_result(self, agent_id, action):
        if not isinstance(agent_id, str):
            raise ProtocolError(INVALID_PARAMS, "agent_id 必须是字符串")
        try:
            payload = action(agent_id)
        except worker_runtime.AgentError as exc:
            return _operation_error(exc)
        except worker_runtime.OperationError as exc:
            return _operation_error(exc)
        return _tool_result(payload)

    # -- stdio 循环 -------------------------------------------------------

    def log(self, text):
        try:
            print(f"{SERVER_NAME}: {text}", file=self.stderr, flush=True)
        except (OSError, ValueError):  # pragma: no cover - 日志失败不影响协议
            pass

    def write(self, message):
        line = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def serve(self, stream):
        for line in stream:
            if not line.strip():
                continue
            if len(line) > MAX_MESSAGE_BYTES:
                self.write({"jsonrpc": "2.0", "id": None,
                            "error": _error(PARSE_ERROR, "Parse error：消息超过大小上限")})
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self.write({"jsonrpc": "2.0", "id": None,
                            "error": _error(PARSE_ERROR, f"Parse error：{exc.msg}")})
                continue
            reply = self.handle(message)
            if reply is not None:
                self.write(reply)
        return 0


def build_server(*, codex_home, command_prefix=None, config_path=None):
    runtime = worker_runtime.Runtime(codex_home=codex_home, command_prefix=command_prefix,
                                     config_path=config_path)
    return Server(runtime)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    return parser.parse_args(argv)


def main_with(server, argv=None):
    """使用给定 Server 实例跑 stdio 循环（供测试包装器在 Python 层注入后端）。"""
    args = parse_arguments(["--codex-home", str(server.runtime.codex_home)] + list(argv or []))
    platform_support.configure_console()
    server.log(f"MCP 服务器已启动：protocol={PROTOCOL_VERSION} codex_home={server.runtime.codex_home}")

    def request_shutdown(_signum, _frame):
        raise KeyboardInterrupt("收到终止信号")

    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - 平台差异
        pass
    status = 0
    try:
        server.serve(sys.stdin)
    except KeyboardInterrupt:
        server.log("收到中断信号；正在终止由本服务器拥有的 worker 进程")
        status = 130
    except BrokenPipeError:
        server.log("stdout 已关闭；正在终止由本服务器拥有的 worker 进程")
    finally:
        try:
            server.runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 - 关闭阶段不再抛出
            server.log(f"清理 worker 时出错：{exc!r}")
        server.log("服务器退出：已清理由本进程启动的 worker")
    return status


def main(argv=None):
    args = parse_arguments(argv)
    server = build_server(codex_home=args.codex_home)
    return main_with(server, argv)


if __name__ == "__main__":
    sys.exit(main())
