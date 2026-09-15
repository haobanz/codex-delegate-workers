#!/usr/bin/env python3
"""真实 Codex CLI worker 后端：偏好解析、进程监督、结果采集与取消。

每次派发都启动一个全新的 ``codex exec`` 进程，沿用用户当前的供应商与认证，
显式传入模型与思考强度；不续接旧会话、不自动重试、不修改全局配置。
返回的是本地进程记录到的启动配置与退出结果，不代表供应商上游的物理模型身份。

隐私边界：工具结果只包含本模块自己产生的字段、有界解析的 CLI 启动头部，以及
``--output-last-message`` 最终输出文件；不解析、不回传 stderr 里的思考、错误或
凭据内容——需要原始诊断时只给出退出码、安全分类和日志路径。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cli_support
import platform_support
import project_rules
import workers


SANDBOXES = ("read-only", "workspace-write")
SANDBOX_DEFAULT = "read-only"
EFFORT_VALUES = sorted(workers.EFFORTS)
STOP_TERM_GRACE_SECONDS = 2.0
STOP_KILL_GRACE_SECONDS = 3.0
STOP_TOTAL_SECONDS = STOP_TERM_GRACE_SECONDS + STOP_KILL_GRACE_SECONDS + 2.0
SHUTDOWN_BUDGET_SECONDS = 30.0
POLL_SECONDS = 0.05
SAMPLE_SECONDS = 0.2
REFRESH_TIMEOUT_SECONDS = 1.0
HEADER_DRAIN_GRACE_SECONDS = 2.0
CONFIG_RELATIVE = Path("skills") / "delegate-workers" / "workers.json"
JOB_PREFIX = "delegate-worker-"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
HEADER_BYTE_LIMIT = 65536
HEADER_LINE_LIMIT = 200
HEADER_START_LIMIT = 12
HEADER_DELIMITER = "--------"
HEADER_PROMPT_MARKER = "user"
HEADER_KEY_KEYS = ("model", "provider", "reasoning effort", "session id")
HEADER_ANY_KEYS = HEADER_KEY_KEYS + ("workdir", "sandbox", "approval", "reasoning summaries")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROJECT_USABLE = "ok"
PROJECT_GLOBAL_FALLBACK = frozenset({"disabled", "not_registered"})
PROC_DIR = Path("/proc")
DEAD_STATES = ("Z", "z", "X", "x")
PROC_TABLE_UNAVAILABLE = "无法读取本机进程表（/proc 与 ps 都不可用）；无法确认 worker 进程是否已停止"
PROC_IDENTITY_UNKNOWN = "已记录后代的进程身份无法比对（缺少启动令牌）；不能据此判定该进程已停止"

IDENTITY_NOTE = "以上字段是本地进程记录到的启动配置与退出结果，不代表供应商上游的物理模型身份。"
WRITABLE_NOTE = "writable_files 只是给 worker 的指令边界，不是文件系统权限 ACL。"
BACKEND_LABEL = "codex exec 独立进程；沿用当前 CODEX_HOME 的供应商与认证"


class ContractError(ValueError):
    """请求参数无法满足（对应 JSON-RPC -32602）。"""


class OperationError(RuntimeError):
    """参数已通过校验，但操作或本机状态失败（对应工具结果 isError）。"""


class AgentError(OperationError):
    """agent_id 未知或不可用。"""


class ProjectStateError(ContractError, OperationError):
    """项目委派状态损坏、漂移或不可读；拒绝静默改用全局默认值。

    同时也是 ``OperationError``，因此只读查询（list_models）会把它记录成
    ``default_error``，而不是让错误逃逸或悄悄换成全局预设。
    """


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strip_ansi(text):
    return ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# CLI 启动头部解析（人类可读 stderr，仅限 prompt 分隔线之前）
# ---------------------------------------------------------------------------


class HeaderScan:
    """解析 CLI 启动头部；prompt 区之后的内容绝不会被当作新鲜元数据。"""

    def __init__(self):
        self.values = {}
        self.status = "unverified"
        self.reason = "未在 CLI 启动头部找到模型与思考强度记录"
        self._state = "before"
        self._lines = 0
        self._bytes = 0
        self._saw_key = False

    def done(self):
        return self._state == "closed"

    def finish(self, reason=None):
        """显式收尾（stderr 提前结束等）；已收尾时保持原状态。"""
        if not self.done():
            self._finish(reason)
        return self.done()

    def feed(self, line):
        if self.done():
            return
        self._lines += 1
        self._bytes += len(line)
        if self._lines > HEADER_LINE_LIMIT or self._bytes > HEADER_BYTE_LIMIT:
            self._finish("启动头部超出记录上限，按未验证处理")
            return
        stripped = line.strip()
        if self._state == "before":
            if stripped == HEADER_DELIMITER:
                self._state = "header"
                return
            if stripped == HEADER_PROMPT_MARKER:
                self._finish("CLI 未提供启动头部（已到 prompt 分隔标记）")
            elif stripped and self._lines > HEADER_START_LIMIT:
                self._finish("CLI 未在起始若干行内提供可识别的启动头部")
            return
        if stripped == HEADER_DELIMITER or stripped == HEADER_PROMPT_MARKER:
            self._finish(None)
            return
        if ":" not in stripped:
            return
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        if key not in HEADER_ANY_KEYS:
            return
        self._saw_key = True
        if key in HEADER_KEY_KEYS:
            self.values[key] = value.strip()

    def _finish(self, reason):
        self._state = "closed"
        if reason is not None:
            self.status = "unverified"
            self.reason = reason
            return
        missing = [key for key in ("model", "reasoning effort") if not self.values.get(key)]
        if missing:
            self.status = "unverified"
            self.reason = "CLI 启动头部缺少字段：" + "、".join(missing)
        else:
            self.status = "recorded"
            self.reason = "已记录 CLI 启动头部中出现的字段；未出现的字段保持未验证"


# ---------------------------------------------------------------------------
# 偏好解析（每次调用都重新读取）
# ---------------------------------------------------------------------------


def check_model(value):
    try:
        return workers.check_model(value)
    except workers.ConfigError as exc:
        raise ContractError(f"模型 ID 无效：{exc}") from exc


def check_effort(value):
    try:
        return workers.check_effort(value)
    except workers.ConfigError as exc:
        raise ContractError(f"思考强度无效：{exc}") from exc


def load_workers_config(config_path):
    """每次都重新读取已安装的模型预设，不使用缓存。"""
    path = Path(config_path)
    if not path.is_file():
        raise OperationError(
            f"找不到已安装的模型预设配置：{path}。请先安装 delegate-workers 技能，"
            "或在 spawn_agent 中显式给出 model 与 reasoning_effort。")
    try:
        return workers.validate_config(workers.read_json(path))
    except workers.ConfigError as exc:
        raise OperationError(f"已安装的模型预设配置无法使用：{exc}") from exc


def compatibility_of(model, effort):
    """静态能力预检；不声称运行时或上游身份已通过探测。"""
    try:
        return workers.validate_worker({"model": model, "reasoning_effort": effort})
    except workers.ConfigError as exc:
        raise ContractError(f"模型与思考强度组合未通过静态能力预检：{exc}") from exc


def _diagnose(model, effort):
    try:
        return compatibility_of(model, effort)
    except ContractError as exc:
        return {"status": "incompatible", "runtime_verified": False, "error": str(exc)}


def project_context(cwd):
    """读取项目委派状态，用于默认值解析与范围提示。

    读取失败会作为 ``available=False`` 记录在这里；默认值解析（project_default）
    必须据此报错，而不是静默改用全局默认值。
    """
    try:
        status = project_rules.status_project(cwd)
    except Exception as exc:  # noqa: BLE001 - 记录为不可用，绝不据此换默认模型
        return {"available": False, "status": "unresolved",
                "error": f"无法读取项目委派状态：{exc}", "warnings": [], "nested_guidance": [],
                "project_root": None, "diagnostics": [], "enabled": False, "git_worktree": False,
                "worker": None, "selection": None, "state_file": None}
    return {
        "available": True,
        "status": status.get("status"),
        "error": None,
        "project_root": status.get("project_root"),
        "state_file": status.get("state_file"),
        "enabled": bool(status.get("enabled")),
        "git_worktree": bool(status.get("git_worktree")),
        "worker": status.get("worker"),
        "selection": status.get("selection"),
        "warnings": list(status.get("scope_warnings") or []),
        "nested_guidance": status.get("nested_guidance") or [],
        "diagnostics": [str(item) for item in (status.get("diagnostics") or [])],
    }


def _project_error(context, status, detail):
    location = (context or {}).get("project_root") or "请求目录"
    return ProjectStateError(
        f"项目委派状态为 {status!r}（损坏、漂移或不可读），已拒绝静默改用全局默认值："
        f"{detail or '状态无效'}。项目：{location}。请先修复项目状态"
        "（dw project status/init/sync），或在 spawn_agent 中显式给出 model 与 reasoning_effort。")


def project_default(context):
    """启用且完整的项目快照优先；已禁用/未登记返回 None；其余状态必须报错。"""
    if not context or not context.get("available"):
        raise _project_error(context, "unresolved",
                             (context or {}).get("error") or "无法确认项目委派状态")
    status = context.get("status")
    if status == PROJECT_USABLE:
        worker = context.get("worker") or {}
        if context.get("enabled") and worker.get("model") and worker.get("reasoning_effort"):
            return {
                "model": worker["model"],
                "reasoning_effort": worker["reasoning_effort"],
                "profile": (context.get("selection") or {}).get("profile"),
                "source": "project-snapshot",
                "source_detail": f"项目 {context.get('project_root')} 中启用且完整的委派快照",
            }
        raise _project_error(context, status or "ok", "项目委派快照缺少模型或思考强度")
    if status in PROJECT_GLOBAL_FALLBACK:
        return None
    detail = "；".join(item for item in (context.get("diagnostics") or []) if item)
    raise _project_error(context, status, detail)


def global_default(config):
    name = config["default_profile"]
    profile = config["profiles"][name]
    return {
        "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"],
        "profile": name,
        "source": "workers.json:default_profile",
        "source_detail": f"已安装 workers.json 的默认预设 {name!r}",
    }


def resolve_selection(cwd, *, profile=None, model=None, effort=None, context=None, config_path=None):
    """解析本次派发的模型与强度；输入覆盖只影响本次调用，不写回任何设置。"""
    if model is not None and not isinstance(model, str):
        raise ContractError("model 必须是字符串")
    if profile is not None and not isinstance(profile, str):
        raise ContractError("profile 必须是字符串")
    if model is not None and effort is None:
        raise ContractError(
            "显式指定 model 时必须同时显式指定 reasoning_effort；不会跨模型沿用其他档位。")
    if model is not None:
        check_model(model)
        check_effort(effort)
        selected = {"model": model, "reasoning_effort": effort, "profile": None,
                    "source": "explicit",
                    "source_detail": "调用方显式给出的模型与强度；未读取项目或全局默认值"}
    elif profile is not None:
        config = load_workers_config(config_path)
        if profile not in config["profiles"]:
            raise ContractError(
                f"未知的模型预设：{profile!r}；当前 workers.json 可用预设："
                f"{', '.join(sorted(config['profiles']))}")
        chosen = config["profiles"][profile]
        selected = {"model": chosen["model"], "reasoning_effort": chosen["reasoning_effort"],
                    "profile": profile, "source": "workers.json:profile",
                    "source_detail": f"调用方显式选择的已安装预设 {profile!r}"}
    else:
        if context is None:
            context = project_context(cwd)
        selected = project_default(context)
        if selected is None:
            selected = global_default(load_workers_config(config_path))
    if effort is not None and model is None:
        check_effort(effort)
        selected = dict(selected)
        selected["reasoning_effort"] = effort
        selected["source"] = selected["source"] + "+effort-override"
        selected["source_detail"] = selected["source_detail"] + "；本次调用只覆盖了思考强度"
    selected["compatibility"] = compatibility_of(selected["model"], selected["reasoning_effort"])
    return selected


# ---------------------------------------------------------------------------
# 进程身份与进程表（POSIX：/proc 优先，ps 兜底；Windows：Job Object）
# ---------------------------------------------------------------------------


def _state_alive(state):
    return bool(state) and state[0] not in DEAD_STATES


def _same_process(entry, token):
    """身份比对：PID 复用后启动时刻会变化，据此避免误杀无关进程。"""
    if token is None or entry[3] is None:
        return False
    return str(entry[3]) == str(token)


def _proc_table_from_proc():
    try:
        entries = os.listdir(PROC_DIR)
    except OSError:
        return None
    table = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = (PROC_DIR / entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        close = raw.rfind(")")
        if close < 0:
            continue
        fields = raw[close + 2:].split()
        if len(fields) < 20:
            continue
        try:
            table[int(entry)] = (int(fields[1]), int(fields[2]), fields[0], fields[19])
        except ValueError:
            continue
    return table or None


def _proc_table_from_ps():
    commands = (["ps", "-eo", "pid=,ppid=,pgid=,state=,lstart="],
                ["ps", "-eo", "pid=,ppid=,pgid=,state="])
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=5, shell=False, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        table = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            try:
                pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            table[pid] = (ppid, pgid, parts[3], parts[4].strip() if len(parts) > 4 else None)
        if table:
            return table
    return {}


def _proc_table():
    """返回 {pid: (ppid, pgid, state, start_token)}。

    Windows 上返回空表；POSIX 上两个来源都不可用时抛出 OSError，让调用方把
    “查不到”当成“无法确认”而不是“已停止”。
    """
    if os.name != "posix":
        return {}
    table = _proc_table_from_proc() or _proc_table_from_ps()
    if not table:
        # 两个来源都不可用：不能把“查不到”当成“已停止”的证据。
        raise OSError(PROC_TABLE_UNAVAILABLE)
    return table


def _descendant_pids(table, root):
    children = {}
    for pid, (ppid, _pgid, _state, _token) in table.items():
        children.setdefault(ppid, set()).add(pid)
    found = set()
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in found and child != root:
                found.add(child)
                frontier.append(child)
    return found


JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_EXTENDED_LIMIT_INFORMATION = 9
JOB_BASIC_ACCOUNTING_INFORMATION = 1
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def _windows_types(ctypes, wintypes):
    """把 wintypes 里在 32/64 位下含义不同的类型固定成明确的指针宽度。

    ``wintypes.LARGE_INTEGER`` 与 64 位指针宽度在不同 Python/平台组合下并不一致，
    Job Object 结构靠它计算布局，必须显式声明为 64 位。
    """
    return {
        "HANDLE": wintypes.HANDLE,
        "LARGE_INTEGER": ctypes.c_longlong,
        "ULONG_PTR": ctypes.c_size_t,
        "SIZE_T": ctypes.c_size_t,
        "DWORD": wintypes.DWORD,
        "BOOL": wintypes.BOOL,
        "UINT": wintypes.UINT,
        "LPVOID": ctypes.c_void_p,
    }


def _windows_job_structures(ctypes, types):
    """构造 Job Object 相关结构；字段布局按 WinAPI 的 64 位宽度显式给出。"""

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOB_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", types["LARGE_INTEGER"]),
                    ("PerJobUserTimeLimit", types["LARGE_INTEGER"]),
                    ("LimitFlags", types["DWORD"]),
                    ("MinimumWorkingSetSize", types["SIZE_T"]),
                    ("MaximumWorkingSetSize", types["SIZE_T"]),
                    ("ActiveProcessLimit", types["DWORD"]),
                    ("Affinity", types["ULONG_PTR"]),
                    ("PriorityClass", types["DWORD"]),
                    ("SchedulingClass", types["DWORD"])]

    class JOB_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOB_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", types["SIZE_T"]),
                    ("JobMemoryLimit", types["SIZE_T"]),
                    ("PeakProcessMemoryUsed", types["SIZE_T"]),
                    ("PeakJobMemoryUsed", types["SIZE_T"])]

    class JOB_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [("TotalUserTime", types["LARGE_INTEGER"]),
                    ("TotalKernelTime", types["LARGE_INTEGER"]),
                    ("ThisPeriodTotalUserTime", types["LARGE_INTEGER"]),
                    ("ThisPeriodTotalKernelTime", types["LARGE_INTEGER"]),
                    ("TotalPageFaultCount", types["DWORD"]),
                    ("TotalProcesses", types["DWORD"]),
                    ("ActiveProcesses", types["DWORD"]),
                    ("TotalTerminatedProcesses", types["DWORD"])]

    return {"extended_limit": JOB_EXTENDED_LIMIT_INFORMATION,
            "accounting": JOB_BASIC_ACCOUNTING_INFORMATION}


def _bind_windows_kernel32(ctypes, wintypes, dll_factory=None):
    """绑定 kernel32 并显式声明每个函数的参数与返回类型。

    不声明 restype 时 ctypes 默认按 32 位 int 处理返回值，64 位句柄会被截断，
    CloseHandle/TerminateJobObject 会作用到错误的句柄上。
    """
    factory = dll_factory or ctypes.WinDLL
    kernel32 = factory("kernel32", use_last_error=True)
    types = _windows_types(ctypes, wintypes)
    signatures = {
        "CreateJobObjectW": ((types["LPVOID"], wintypes.LPCWSTR), types["HANDLE"]),
        "SetInformationJobObject": ((types["HANDLE"], ctypes.c_int, types["LPVOID"],
                                     types["DWORD"]), types["BOOL"]),
        "QueryInformationJobObject": ((types["HANDLE"], ctypes.c_int, types["LPVOID"],
                                       types["DWORD"], types["LPVOID"]), types["BOOL"]),
        "TerminateJobObject": ((types["HANDLE"], types["UINT"]), types["BOOL"]),
        "AssignProcessToJobObject": ((types["HANDLE"], types["HANDLE"]), types["BOOL"]),
        "OpenProcess": ((types["DWORD"], types["BOOL"], types["DWORD"]), types["HANDLE"]),
        "CloseHandle": ((types["HANDLE"],), types["BOOL"]),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(kernel32, name, None)
        if function is None:  # pragma: no cover - 非 Windows 或异常安装
            raise OSError(f"kernel32 缺少函数：{name}")
        function.argtypes = list(argtypes)
        function.restype = restype
    return kernel32, types


class WindowsJob:
    """Windows Job Object：终止/关闭作业即终止其中的整棵子进程树。"""

    def __init__(self, kernel32=None, structures=None):
        import ctypes
        from ctypes import wintypes

        if kernel32 is None:
            kernel32, types = _bind_windows_kernel32(ctypes, wintypes)
        else:
            types = _windows_types(ctypes, wintypes)
        structures = structures or _windows_job_structures(ctypes, types)
        self._ctypes = ctypes
        self._types = types
        self._EXTENDED_LIMIT = structures["extended_limit"]
        self._ACCOUNTING = structures["accounting"]
        self._kernel32 = kernel32
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")
        info = self._EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
                self.handle, JOB_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject 失败")

    @classmethod
    def create(cls):
        try:
            return cls()
        except Exception:  # noqa: BLE001 - 无 Job 能力时回退到 taskkill
            return None

    def assign(self, pid):
        ctypes = self._ctypes
        handle = self._kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self.handle, handle))
        finally:
            self._kernel32.CloseHandle(handle)

    def active_processes(self):
        """作业中仍然存活的进程数；查询失败返回 None（不得当作已停止）。"""
        info = self._ACCOUNTING()
        ok = self._kernel32.QueryInformationJobObject(
            self.handle, JOB_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(info), self._ctypes.sizeof(info), None)
        if not ok:
            return None
        return int(info.ActiveProcesses)

    def terminate(self):
        return bool(self._kernel32.TerminateJobObject(self.handle, 0))

    def close(self):
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# Worker 进程
# ---------------------------------------------------------------------------


class Agent:
    """一次派发的适配器状态；agent_id 不是 CLI 会话 ID。"""

    def __init__(self, agent_id, task, cwd, sandbox, writable_files, requested, resolved, job_dir):
        self.agent_id = agent_id
        self.task = task
        self.cwd = cwd
        self.sandbox = sandbox
        self.writable_files = list(writable_files)
        self.requested = requested
        self.resolved = resolved
        self.job_dir = job_dir
        self.status = "running"
        self.output = ""
        self.output_truncated = False
        self.output_note = None
        self.warnings = []
        self.error = None
        self.error_category = None
        self.exit_code = None
        self.started_at = _now()
        self.finished_at = None
        self.proc = None
        self.pgid = None
        self.job = None
        self.descendants = {}
        self.header = HeaderScan()
        self.header_values = {}
        self.header_status = None
        self.header_reason = None
        self.cli_session_id = None
        self.mismatch = None
        self.stop_requested = False
        self.stop_reason = None
        self.stop_in_progress = False
        self.stop_error = None
        self.windows_tree_killed = False
        self.drain_error = None
        self.stderr_stream = None
        self.tree_gone = threading.Event()
        self.finished = threading.Event()
        self.drain_done = threading.Event()
        self.finalizing = False
        self.monitor = None
        self.scope = {}
        self.lock = threading.RLock()

    def paths(self):
        return {
            "job_dir": str(self.job_dir),
            "request": str(self.job_dir / "request.json"),
            "prompt": str(self.job_dir / "prompt.txt"),
            "stdout_log": str(self.job_dir / "worker.stdout.log"),
            "stderr_log": str(self.job_dir / "worker.stderr.log"),
            "last_message": str(self.job_dir / "last-message.txt"),
            "result": str(self.job_dir / "result.json"),
        }


class Runtime:
    """拥有 worker 子进程的运行时；每个 MCP 服务器进程使用一个实例。"""

    def __init__(self, *, codex_home, command_prefix=None, config_path=None):
        self.codex_home = Path(codex_home).expanduser().resolve()
        self.config_path = Path(config_path) if config_path else self.codex_home / CONFIG_RELATIVE
        self._command_prefix = list(command_prefix) if command_prefix else None
        self._agents = {}
        self._lock = threading.Lock()

    # -- 基础 -------------------------------------------------------------

    def command_prefix(self):
        """本机真实 Codex CLI 命令前缀；在创建任何任务产物之前解析并缓存。"""
        if self._command_prefix:
            return list(self._command_prefix)
        try:
            prefix = cli_support.codex_command()
        except cli_support.CliNotFoundError as exc:
            raise OperationError(str(exc)) from exc
        except OSError as exc:
            raise OperationError(f"无法启动本机 Codex CLI：{exc}") from exc
        self._command_prefix = list(prefix)
        return list(prefix)

    def build_command(self, *, model, effort, sandbox, last_message_path, git_worktree=True):
        """构造参数数组（不经过 shell；模型与强度原样传入）。"""
        command = [
            *(self._command_prefix or self.command_prefix()),
            "exec",
            "--ephemeral",
        ]
        if not git_worktree:
            # 只有非 Git 项目才需要放行仓库检查；Git 项目保持 CLI 的默认校验。
            command.append("--skip-git-repo-check")
        command += [
            "--sandbox", sandbox,
            "--model", model,
            "-c", "model_reasoning_effort=" + json.dumps(effort, ensure_ascii=False),
            "--color", "never",
            "--output-last-message", str(last_message_path),
            "-",
        ]
        return command

    def list_models(self, cwd):
        config = load_workers_config(self.config_path)
        context = project_context(cwd)
        profiles = {
            name: {
                "model": profile["model"],
                "reasoning_effort": profile["reasoning_effort"],
                "compatibility": _diagnose(profile["model"], profile["reasoning_effort"]),
            }
            for name, profile in config["profiles"].items()
        }
        default, default_error = None, None
        try:
            default = project_default(context)
            if default is None:
                default = global_default(config)
        except OperationError as exc:
            default_error = str(exc)
        if default is not None:
            default["compatibility"] = _diagnose(default["model"], default["reasoning_effort"])
        return {
            "profiles": profiles,
            "default": default,
            "default_error": default_error,
            "accepts_custom_model": True,
            "hardcoded_native_model_enum": False,
            "catalog_scope": "这里只列出本工具已安装配置中的预设及其静态能力预检结果；"
                             "它不是供应商可用模型目录，也不代表运行时或上游身份已经验证。",
            "sources": {
                "workers_config": str(self.config_path),
                "project_state_file": context.get("state_file"),
                "project_root": context.get("project_root"),
                "project_status": context.get("status"),
                "project_error": context.get("error"),
            },
            "scope_warnings": context.get("warnings") or [],
            "nested_guidance": context.get("nested_guidance") or [],
        }

    # -- 查询 -------------------------------------------------------------

    def _refresh(self, agent, timeout=REFRESH_TIMEOUT_SECONDS):
        """报告状态前给监督线程一次有界机会收尾；绝不在此处伪造终态。"""
        if agent.status != "running":
            return
        if agent.proc is not None and agent.proc.poll() is None:
            return
        if agent.finished.wait(timeout=timeout):
            return
        # 监督线程可能已异常退出：兜底收敛一次；无法确认停止时保持 running。
        self._converge(agent)

    def get(self, agent_id):
        agent = self._agent(agent_id)
        self._refresh(agent)
        return self._snapshot(agent)

    def list(self):
        with self._lock:
            agents = list(self._agents.values())
        for agent in agents:
            self._refresh(agent)
        return {"agents": [self._snapshot(agent) for agent in agents], "count": len(agents)}

    def cancel(self, agent_id):
        agent = self._agent(agent_id)
        self._refresh(agent)
        with agent.lock:
            if agent.status != "running":
                return self._snapshot(agent, cancel_note=
                                      f"任务已处于终态 {agent.status}；没有终止任何进程，保留其真实结果。")
            if agent.proc is None or agent.proc.poll() is not None:
                stop_requested = False
            else:
                stop_requested = True
                agent.stop_requested = True
                agent.stop_reason = "cancel"
        if not stop_requested:
            agent.finished.wait(timeout=STOP_TERM_GRACE_SECONDS)
            return self._snapshot(agent, cancel_note="子进程已经自行结束；未再发送信号，保留其真实结果。")
        self._stop_tree(agent)  # 阻塞至确认停止或等待上限；重复调用并入同一次终止
        if not agent.finished.is_set():
            self._converge(agent)  # 仍有残留后代时继续按身份清理，无法确认则记录 stop_error
            agent.finished.wait(timeout=STOP_TERM_GRACE_SECONDS)
        if agent.status == "running" and not agent.stop_error:
            with agent.lock:
                agent.stop_error = ("无法在等待上限内确认 worker 进程树已停止；状态保持 running，"
                                    "请先人工确认相关进程后再继续，避免重叠写入。")
        snapshot = self._snapshot(agent)
        if agent.status == "running":
            snapshot["cancel_note"] = ("已请求终止，但在等待上限内无法确认整棵进程树都已停止；"
                                       "状态保持 running，请先人工确认后再继续，避免重叠写入。")
        elif agent.status == "cancelled":
            snapshot["cancel_note"] = ("已确认 worker 进程树停止后才返回终态；"
                                       "晚期取消不会把已完成或失败的真实结果改写成 cancelled。")
        else:
            snapshot["cancel_note"] = f"任务在终止生效前已进入终态 {agent.status}；保留其真实结果。"
        return snapshot

    def shutdown(self):
        """服务器关闭（stdin EOF / SIGTERM / Ctrl-C）时清理仍然存活的子进程。

        先对所有 worker 同时发出停止请求，再在统一预算内收集结果，避免
        逐个等待导致总时长随任务数增长。
        """
        with self._lock:
            agents = list(self._agents.values())
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        pending = []
        threads = []
        for agent in agents:
            with agent.lock:
                if agent.status != "running":
                    continue
                if agent.proc is not None and agent.proc.poll() is None:
                    agent.stop_requested = True
                    agent.stop_reason = agent.stop_reason or "shutdown"
            pending.append(agent)
            thread = threading.Thread(target=self._stop_tree, args=(agent,), daemon=True)
            thread.start()
            threads.append(thread)
        for agent in pending:
            remaining = max(0.0, deadline - time.monotonic())
            if not agent.finished.wait(timeout=min(remaining, STOP_TERM_GRACE_SECONDS + STOP_KILL_GRACE_SECONDS)):
                self._converge(agent, deadline=deadline)
            if agent.monitor is not None:
                agent.monitor.join(timeout=0.5)
        for thread in threads:
            thread.join(timeout=1.0)
        for agent in pending:
            self._release_job(agent)

    # -- 派发 -------------------------------------------------------------

    def spawn(self, *, task, cwd, profile=None, model=None, effort=None, sandbox=SANDBOX_DEFAULT,
              writable_files=()):
        if not isinstance(task, str) or not task.strip():
            raise ContractError("task 必须是非空字符串")
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_absolute():
            raise ContractError(f"cwd 必须是绝对路径：{cwd}")
        if not cwd_path.is_dir():
            raise ContractError(f"cwd 不是已存在的目录：{cwd_path}")
        cwd_path = cwd_path.resolve()
        if sandbox not in SANDBOXES:
            raise ContractError(f"sandbox 取值无效：{sandbox!r}；可选：{', '.join(SANDBOXES)}")
        if not isinstance(writable_files, (list, tuple)) or any(
                not isinstance(item, str) for item in writable_files):
            raise ContractError("writable_files 必须是字符串数组")
        command_prefix = self.command_prefix()  # 找不到本机 CLI 时不留下任何任务产物
        context = project_context(cwd_path)
        resolved = resolve_selection(cwd_path, profile=profile, model=model, effort=effort,
                                     context=context, config_path=self.config_path)
        agent_id = "wk-" + uuid.uuid4().hex[:12]
        job_dir = _job_dir(cwd_path, context.get("project_root"), agent_id)
        requested = {"task": task, "cwd": str(cwd_path), "profile": profile, "model": model,
                     "reasoning_effort": effort, "sandbox": sandbox,
                     "writable_files": list(writable_files)}
        agent = Agent(agent_id, task, str(cwd_path), sandbox, writable_files, requested, resolved,
                      job_dir)
        agent.scope = {
            "project_root": context.get("project_root"),
            "project_status": context.get("status"),
            "git_worktree": context.get("git_worktree", False),
            "scope_warnings": context.get("warnings") or [],
            "nested_guidance": context.get("nested_guidance") or [],
        }
        if not context.get("available"):
            agent.scope["scope_warnings"] = list(agent.scope["scope_warnings"]) + [
                "无法读取项目委派状态；本次未使用项目快照，模型与强度来自显式输入或已安装预设"]
        self._start(agent)
        with self._lock:
            self._agents[agent_id] = agent
        monitor = threading.Thread(target=self._supervise, args=(agent,), daemon=True)
        drain = threading.Thread(target=self._drain_stderr, args=(agent,), daemon=True)
        agent.monitor = monitor
        try:
            drain.start()
            monitor.start()
        except Exception as exc:  # noqa: BLE001 - 线程无法启动时绝不留下无人监督的子进程
            with agent.lock:
                agent.stop_requested = True
                agent.stop_reason = agent.stop_reason or "supervision_failure"
            confirmed = self._stop_tree(agent)
            if confirmed:
                self._converge(agent)
                with self._lock:
                    self._agents.pop(agent_id, None)
                raise OperationError(f"无法启动 worker 监督线程；已确认终止子进程：{exc}") from exc
            # 无法确认停止：保留该 worker 可见，让调用方能够查询和再次取消。
            with agent.lock:
                agent.stop_error = "无法启动监督线程，且未能确认子进程已停止；请检查后再继续"
            raise OperationError(
                f"无法启动 worker 监督线程，且未能确认子进程已停止（agent_id={agent_id}，"
                f"pid={agent.proc.pid if agent.proc else None}）；已保留该 worker 供查询与取消：{exc}")
        return self._snapshot(agent)

    def _start(self, agent):
        paths = agent.paths()
        last_message = Path(paths["last_message"])
        if os.path.lexists(last_message):
            raise OperationError(f"任务产物路径已被占用，未启动子进程：{last_message}")
        command = self.build_command(model=agent.resolved["model"],
                                     effort=agent.resolved["reasoning_effort"],
                                     sandbox=agent.sandbox, last_message_path=last_message,
                                     git_worktree=bool(agent.scope.get("git_worktree", True)))
        prompt = _compose_prompt(agent)
        try:
            Path(paths["prompt"]).write_text(prompt, encoding="utf-8")
            Path(paths["request"]).write_text(json.dumps({
                "agent_id": agent.agent_id,
                "command": command,
                "route": {
                    "cli": "codex exec（独立进程，--ephemeral）",
                    "model": agent.resolved["model"],
                    "reasoning_effort": agent.resolved["reasoning_effort"],
                    "selection_source": agent.resolved["source"],
                    "provider": "沿用当前 CODEX_HOME 中已有供应商与认证；未复制凭据、未直连供应商 API",
                    "aliases": False,
                },
                "sandbox": agent.sandbox,
                "writable_files": agent.writable_files,
                "writable_files_note": WRITABLE_NOTE,
                "cwd": agent.cwd,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            stderr_log = open(paths["stderr_log"], "w", encoding="utf-8")
        except OSError as exc:
            raise OperationError(f"无法准备任务产物文件（未启动子进程）：{exc}") from exc
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.codex_home)
        for name in ("TMPDIR", "TMP", "TEMP"):
            environment[name] = str(agent.job_dir)
        kwargs = {"cwd": agent.cwd, "env": environment, "shell": False,
                  "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        stdin_stream = stdout_stream = None
        try:
            stdin_stream = open(paths["prompt"], "rb")
            stdout_stream = open(paths["stdout_log"], "wb")
            agent.proc = subprocess.Popen(command, stdin=stdin_stream, stdout=stdout_stream,
                                          stderr=subprocess.PIPE, **kwargs)
        except OSError as exc:
            stderr_log.close()
            agent.status = "failed"
            agent.error_category = "cli_start_failed"
            agent.error = f"无法启动本机 Codex CLI（未留下运行中的子进程）：{exc}"
            agent.finished_at = _now()
            agent.tree_gone.set()
            agent.finished.set()
            raise OperationError(agent.error) from exc
        finally:
            for stream in (stdin_stream, stdout_stream):
                if stream is not None:
                    stream.close()
        agent.stderr_stream = stderr_log
        try:
            agent.pgid = os.getpgid(agent.proc.pid) if os.name == "posix" else agent.proc.pid
        except OSError:  # pragma: no cover - 极端竞态
            agent.pgid = agent.proc.pid
        if os.name == "nt":  # pragma: no cover - 仅在 Windows 上执行
            job = WindowsJob.create()
            if job is not None:
                if job.assign(agent.proc.pid):
                    agent.job = job
                else:
                    job.close()

    # -- 内部 -------------------------------------------------------------

    def _agent(self, agent_id):
        if not isinstance(agent_id, str):
            raise AgentError("agent_id 必须是字符串")
        with self._lock:
            agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentError(f"未知的 agent_id：{agent_id!r}")
        return agent

    def _drain_stderr(self, agent):
        """只负责把 stderr 写入受限日志并解析启动头部；监督与终止在独立线程。"""
        stream = getattr(agent, "stderr_stream", None)
        try:
            if stream is None:
                stream = open(agent.paths()["stderr_log"], "w", encoding="utf-8")
                agent.stderr_stream = stream
            for line in agent.proc.stderr:
                stream.write(line)
                stream.flush()
                agent.header.feed(line)
                if agent.header.done() and agent.header_status is None:
                    self._record_header(agent)
                    if agent.mismatch is not None:
                        self._request_stop(agent, "mismatch")
        except Exception as exc:  # noqa: BLE001 - 排空失败必须终止子进程
            with agent.lock:
                agent.drain_error = f"读取 CLI stderr 失败：{exc}"
            print(f"delegate-workers: 读取 CLI stderr 失败：{exc}", file=sys.stderr, flush=True)
            with agent.lock:
                if agent.status == "running":
                    agent.stop_requested = True
                    agent.stop_reason = agent.stop_reason or "log_failure"
            self._stop_tree(agent)
        finally:
            try:
                if not agent.header.done():
                    agent.header.finish("CLI stderr 在启动头部结束前关闭，按未验证处理")
                if agent.header_status is None:
                    self._record_header(agent)
            except Exception:  # noqa: BLE001 - 收尾解析失败按未验证处理
                pass
            for target in (agent.proc.stderr, stream):
                try:
                    if target is not None:
                        target.close()
                except Exception:  # noqa: BLE001
                    pass
            agent.drain_done.set()

    def _record_header(self, agent):
        values = dict(agent.header.values)
        agent.header_values = values
        agent.header_status = agent.header.status
        agent.header_reason = agent.header.reason
        if values.get("session id"):
            agent.cli_session_id = values["session id"]
        problems = []
        recorded_model = values.get("model")
        recorded_effort = values.get("reasoning effort")
        if recorded_model and recorded_model != agent.resolved["model"]:
            problems.append(f"model 记录为 {recorded_model!r}，本次请求为 {agent.resolved['model']!r}")
        if recorded_effort and recorded_effort.casefold() != agent.resolved["reasoning_effort"].casefold():
            problems.append(f"reasoning effort 记录为 {recorded_effort!r}，"
                            f"本次请求为 {agent.resolved['reasoning_effort']!r}")
        if problems:
            agent.mismatch = "；".join(problems)

    def _request_stop(self, agent, reason):
        """从排空线程请求停止：放到独立线程执行，避免阻塞 stderr 排空。"""
        with agent.lock:
            if agent.status != "running" or agent.stop_requested:
                return
            agent.stop_requested = True
            agent.stop_reason = reason
        threading.Thread(target=self._stop_tree, args=(agent,), daemon=True).start()

    def _supervise(self, agent):
        """生命周期监督：采样后代身份，等待组长退出，然后收敛终态。

        该线程不依赖 stderr 排空：即使某个后代一直持有 stderr 管道，
        组长退出也能被及时发现并收尾。
        """
        try:
            while agent.proc.poll() is None:
                try:
                    self._track_descendants(agent)
                except Exception:  # noqa: BLE001 - 采样失败不影响监督
                    pass
                time.sleep(SAMPLE_SECONDS)
            try:
                self._track_descendants(agent)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 监督线程不能抛出
            print(f"delegate-workers: 进程监督失败：{exc}", file=sys.stderr, flush=True)
            with agent.lock:
                if agent.status == "running":
                    agent.stop_requested = True
                    agent.stop_reason = agent.stop_reason or "supervision_failure"
        try:
            self._converge(agent)
        except Exception as exc:  # noqa: BLE001
            print(f"delegate-workers: 收敛终态失败：{exc}", file=sys.stderr, flush=True)
            with agent.lock:
                if agent.status == "running":
                    agent.stop_error = f"进程监督收尾失败：{exc}"

    def _track_descendants(self, agent):
        """在组长仍存活时记录后代身份，供取消/收尾时按身份终止。"""
        if os.name != "posix":
            return
        proc = agent.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            table = _proc_table()
        except OSError:  # 进程表不可用：放弃补记，不据此推断任何进程状态
            return
        if not table:
            return
        for pid in _descendant_pids(table, proc.pid):
            entry = table.get(pid)
            if entry is not None:
                agent.descendants.setdefault(pid, entry[3])

    def _converge(self, agent, deadline=None):
        """组长退出后收敛终态；无法确认停止时保持 running 并记录明确错误。

        调用前提是组长已经退出（或从未启动）。组长退出时仍有后代存活，说明
        这些后代是被遗留的写入者，必须按记录到的身份终止，不能带着它们收尾。
        """
        if agent.status != "running":
            return True
        if agent.proc is not None and agent.proc.poll() is None:
            return None
        try:
            self._track_descendants(agent)  # 组长刚落幕：尽量再补记一次后代身份
        except Exception:  # noqa: BLE001 - 采样失败不影响收敛
            pass
        limit = time.monotonic() + STOP_TOTAL_SECONDS
        if deadline is not None:
            limit = min(limit, deadline)
        while True:
            try:
                _leader_exited, remaining = self._tree_report(agent)
            except Exception as exc:  # noqa: BLE001 - 无法确认一律按未停止处理
                with agent.lock:
                    agent.stop_error = f"无法确认 worker 进程树状态：{exc}"
                return None
            if not remaining:
                agent.tree_gone.set()
                with agent.lock:
                    agent.stop_error = None
                return self._finalize(agent)
            with agent.lock:
                if not agent.stop_requested:
                    agent.stop_requested = True
                    agent.stop_reason = agent.stop_reason or "orphaned_descendants"
                    agent.warnings.append("组长退出后仍发现残留后代进程，已按记录到的身份终止")
            try:
                self._stop_tree(agent)
            except Exception as exc:  # noqa: BLE001 - 停止失败按未确认处理
                with agent.lock:
                    agent.stop_error = f"终止 worker 进程树时出错：{exc}"
                return None
            if agent.tree_gone.is_set():
                continue  # 重新核对一次再收尾
            if time.monotonic() >= limit:
                with agent.lock:
                    agent.stop_error = ("无法在等待上限内确认 worker 进程树已停止；"
                                        "状态保持 running，请先人工确认相关进程后再继续，避免重叠写入。")
                return None
            time.sleep(POLL_SECONDS)

    def _finalize(self, agent):
        """把已确认停止的 running 收敛成终态；同一任务只有第一个调用者真正完成。"""
        with agent.lock:
            if agent.status != "running":
                return True
            if agent.finalizing:
                return False
            agent.finalizing = True
        try:
            # 快速退出竞态：组长可能在任何 stderr 行被处理前就结束。先给排空线程
            # 一个有界机会收尾，避免在启动头部（含模型不一致）解析前就宣布完成。
            leader_exited = agent.proc is None or agent.proc.poll() is not None
            if agent.proc is not None and leader_exited and not agent.drain_done.is_set():
                if not agent.drain_done.wait(timeout=HEADER_DRAIN_GRACE_SECONDS):
                    with agent.lock:
                        agent.stop_error = "CLI 启动记录仍在处理，任务保持 running；请稍后再次查询。"
                    return False
            with agent.lock:
                if agent.stop_error == "CLI 启动记录仍在处理，任务保持 running；请稍后再次查询。":
                    agent.stop_error = None
            exit_code = agent.proc.returncode if agent.proc is not None else None
            output, output_meta = _read_output(Path(agent.paths()["last_message"]))
            with agent.lock:
                if agent.status != "running":
                    return True
                agent.exit_code = exit_code
                agent.output = output
                agent.output_truncated = bool(output_meta["truncated"])
                agent.output_note = output_meta["note"]
                if agent.header_status is None:
                    agent.header_values = dict(agent.header.values)
                    agent.header_status = agent.header.status
                    agent.header_reason = agent.header.reason
                    if agent.header.values.get("session id"):
                        agent.cli_session_id = agent.header.values["session id"]
                if agent.mismatch is not None:
                    agent.status = "failed"
                    agent.error_category = "metadata_mismatch"
                    agent.error = ("CLI 启动头部记录的模型或思考强度与本次请求不一致（"
                                   + agent.mismatch + "），已停止子进程并按失败报告，"
                                   "没有继续交付工作。原始诊断日志："
                                   + agent.paths()["stderr_log"])
                elif agent.stop_requested and exit_code != 0:
                    agent.status = "cancelled"
                    agent.error_category = "cancelled"
                    agent.error = f"已按取消请求终止 worker 进程树（退出码 {exit_code}）。"
                elif exit_code == 0 and output:
                    agent.status = "completed"
                    agent.error = None
                    agent.error_category = None
                elif exit_code == 0:
                    agent.status = "failed"
                    agent.error_category = "missing_final_message"
                    agent.error = ("Codex CLI 以退出码 0 结束，但没有产生 --output-last-message 最终输出"
                                   f"（{output_meta['error'] or '文件为空或缺失'}）；按失败处理，"
                                   f"没有自动重试。原始日志见 {agent.paths()['stderr_log']}。")
                else:
                    agent.status = "failed"
                    agent.error_category = "cli_nonzero_exit"
                    agent.error = (f"worker 进程以退出码 {exit_code} 结束；未解析、未回传 stderr 内容，"
                                   f"原始诊断日志见 {agent.paths()['stderr_log']}（不会自动重试）。")
                warnings = list(agent.warnings)
                if agent.output_truncated:
                    warnings.append(agent.output_note or "最终输出已按上限截断")
                if agent.stop_requested and not agent.tree_gone.is_set():
                    warnings.append("未能确认 worker 进程树已完全停止")
                agent.warnings = warnings
                agent.finished_at = _now()
                agent.finalizing = False
            self._write_result(agent)
            agent.finished.set()
            return True
        finally:
            with agent.lock:
                agent.finalizing = False
            self._release_job(agent)

    def _write_result(self, agent):
        try:
            Path(agent.paths()["result"]).write_text(
                json.dumps(self._snapshot(agent), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 证据文件失败不影响状态
            print(f"delegate-workers: 结果文件写入失败：{exc}", file=sys.stderr, flush=True)

    def _release_job(self, agent):
        job = agent.job
        if job is not None:
            try:
                job.close()
            finally:
                agent.job = None

    def _snapshot(self, agent, cancel_note=None):
        with agent.lock:
            return self._snapshot_locked(agent, cancel_note)

    def _snapshot_locked(self, agent, cancel_note=None):
        return {
            "backend": BACKEND_LABEL,
            "agent_id": agent.agent_id,
            "cli_session_id": agent.cli_session_id,
            "cli_session_id_source": "CLI 启动头部记录" if agent.cli_session_id else None,
            "status": agent.status,
            "requested": agent.requested,
            "resolved": agent.resolved,
            "observed": {
                "values": agent.header_values,
                "status": agent.header_status or "unverified",
                "reason": agent.header_reason,
                "note": "启动头部只是记录到的配置，不代表上游物理身份；缺失元数据按未验证处理，"
                        "不视为失败。",
            },
            "task": agent.task,
            "cwd": agent.cwd,
            "sandbox": agent.sandbox,
            "writable_files": agent.writable_files,
            "writable_files_note": WRITABLE_NOTE,
            "project_root": agent.scope.get("project_root"),
            "project_status": agent.scope.get("project_status"),
            "scope_warnings": agent.scope.get("scope_warnings") or [],
            "nested_guidance": agent.scope.get("nested_guidance") or [],
            "output": agent.output,
            "output_truncated": agent.output_truncated,
            "output_limit_bytes": MAX_OUTPUT_BYTES,
            "output_note": agent.output_note,
            "warnings": list(agent.warnings),
            "error": agent.error,
            "error_category": agent.error_category,
            "stop_error": agent.stop_error,
            "exit_code": agent.exit_code,
            "pid": agent.proc.pid if agent.proc else None,
            "started_at": agent.started_at,
            "finished_at": agent.finished_at,
            "tree_stopped": agent.tree_gone.is_set(),
            "tree_stopped_scope": ("tree_stopped 只表示本运行时可观察到的进程组与已记录后代都已停止；"
                                   "未观察到、完全自治的守护进程不在证明范围内。"),
            "cancel_note": cancel_note if cancel_note is not None else None,
            "artifacts": agent.paths(),
            "identity_note": IDENTITY_NOTE,
        }

    # -- 终止进程树 -------------------------------------------------------

    def _tree_report(self, agent):
        """返回 (组长已退出, 仍存活的受管进程标识列表)。"""
        proc = agent.proc
        leader_exited = proc is None or proc.poll() is not None
        if os.name != "posix":  # pragma: no cover - 仅在 Windows 上执行
            return self._windows_tree_report(agent, leader_exited)
        table = _proc_table()
        remaining = set()
        for pid, token in agent.descendants.items():
            entry = table.get(pid)
            if entry is None:
                continue
            if token is not None and entry[3] is not None:
                if str(entry[3]) == str(token):
                    if _state_alive(entry[2]):
                        remaining.add(pid)
                    continue
                # 启动令牌明确不同：该 PID 已被复用，原后代已不存在。
                continue
            # 身份令牌缺失，无法比对：除非它独立处于受管进程组内（由下面统一覆盖），
            # 否则不能把“比对不了”当成“已停止”。
            if agent.pgid and entry[1] == agent.pgid:
                continue
            if _state_alive(entry[2]):
                raise OSError(PROC_IDENTITY_UNKNOWN)
        pgid = agent.pgid
        if pgid:
            for pid, entry in table.items():
                if entry[1] == pgid and _state_alive(entry[2]):
                    remaining.add(pid)
        if proc is not None and proc.poll() is None:
            remaining.add(proc.pid)
        return leader_exited, sorted(remaining)

    def _windows_tree_report(self, agent, leader_exited):  # pragma: no cover - Windows
        job = agent.job
        if job is not None:
            active = job.active_processes()
            if active is None:
                return leader_exited, [f"job:{agent.agent_id}"]
            return leader_exited, ([] if active == 0 else [f"job:{agent.agent_id}:{active}"])
        if agent.windows_tree_killed:
            return leader_exited, []
        return leader_exited, ([f"unconfirmed:{agent.agent_id}"] if leader_exited else [])

    def _tree_live(self, agent):
        _leader_exited, remaining = self._tree_report(agent)
        return bool(remaining)

    def _stop_tree(self, agent):
        """终止整棵进程树；只在能确认停止时返回 True（并设置 tree_gone）。"""
        with agent.lock:
            if agent.tree_gone.is_set():
                return True
            if agent.stop_in_progress:
                waiter = False
            else:
                agent.stop_in_progress = True
                waiter = True
        if not waiter:
            agent.tree_gone.wait(timeout=STOP_TOTAL_SECONDS)
            return agent.tree_gone.is_set()
        try:
            confirmed = self._stop_tree_locked(agent)
            if confirmed:
                agent.tree_gone.set()
            return confirmed
        except OSError as exc:  # 无法确认进程表/身份时保持未停止，不伪造终态
            with agent.lock:
                agent.stop_error = f"无法确认 worker 进程树状态：{exc}"
            return False
        finally:
            with agent.lock:
                agent.stop_in_progress = False

    def _stop_tree_locked(self, agent):
        proc = agent.proc
        if proc is None:
            agent.tree_gone.set()
            return True
        if os.name == "posix":
            return self._stop_tree_posix(agent, proc)
        return self._stop_tree_windows(agent, proc)  # pragma: no cover - Windows

    def _stop_tree_posix(self, agent, proc):
        self._track_descendants(agent)
        self._signal_tree(agent, signal.SIGTERM)
        if self._wait_tree_report(agent, STOP_TERM_GRACE_SECONDS):
            return True
        # 宽限期内组长可能仍存活：再采样一次，尽量发现新出现的后代。
        self._track_descendants(agent)
        self._signal_tree(agent, signal.SIGKILL)
        return self._wait_tree_report(agent, STOP_KILL_GRACE_SECONDS)

    def _signal_tree(self, agent, signum):
        pgid = agent.pgid
        if pgid:
            try:
                os.killpg(pgid, signum)
            except ProcessLookupError:
                pass
            except OSError:  # pragma: no cover - 权限或其他系统错误
                pass
        proc = agent.proc
        if proc is not None and proc.poll() is None and not pgid:
            try:
                proc.send_signal(signum)
            except OSError:
                pass
        table = {}
        if agent.descendants:
            try:
                table = _proc_table() or {}
            except OSError:
                # 无法验证身份时只保留上面已发出的进程组信号，
                # 不按 PID 猜测终止可能已被复用的独立会话进程。
                table = {}
        for pid, token in list(agent.descendants.items()):
            entry = table.get(pid)
            if entry is None or not _same_process(entry, token) or not _state_alive(entry[2]):
                continue
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
            except OSError:  # pragma: no cover - 权限或其他系统错误
                pass

    def _wait_tree_report(self, agent, timeout):
        deadline = time.monotonic() + timeout
        while True:
            leader_exited, remaining = self._tree_report(agent)
            if leader_exited and not remaining:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS)

    def _stop_tree_windows(self, agent, proc):  # pragma: no cover - Windows only
        """先处理整棵树的终止，再判断组长是否退出；不因组长退出就宣称树已停止。"""
        job = agent.job
        if job is not None:
            job.terminate()
            deadline = time.monotonic() + STOP_TERM_GRACE_SECONDS + STOP_KILL_GRACE_SECONDS
            while time.monotonic() < deadline:
                active = job.active_processes()
                if active == 0:
                    break
                time.sleep(POLL_SECONDS)
            job.terminate()
        if proc.poll() is None:
            # 组长仍存活时运行 taskkill /T /F，才能覆盖其子进程树。
            try:
                result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                        capture_output=True, shell=False, check=False,
                                        timeout=STOP_KILL_GRACE_SECONDS + STOP_TERM_GRACE_SECONDS)
                agent.windows_tree_killed = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                agent.windows_tree_killed = False
        try:
            proc.wait(timeout=STOP_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=STOP_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        if agent.job is not None:
            active = agent.job.active_processes()
            if active is None:
                return False
            return active == 0
        return agent.windows_tree_killed


def _job_dir(cwd, project_root, agent_id):
    """在项目根 tmp 下创建任务产物目录，并拒绝符号链接重定向。"""
    try:
        if project_root:
            candidate = Path(project_root) / "tmp"
            if candidate.is_symlink():
                raise OperationError(f"项目临时目录是符号链接，未使用：{candidate}")
            candidate.mkdir(parents=True, exist_ok=True)
            if candidate.is_symlink() or not candidate.is_dir():
                raise OperationError(f"项目临时目录不可信，未启动子进程：{candidate}")
        else:
            candidate = platform_support.project_tmp(cwd)
    except OSError as exc:
        raise OperationError(f"无法准备项目临时目录：{exc}") from exc
    job_dir = candidate / (JOB_PREFIX + agent_id.split("-", 1)[-1])
    if os.path.lexists(job_dir):
        raise OperationError(f"任务产物目录已存在，未覆盖：{job_dir}")
    try:
        job_dir.mkdir(exist_ok=False)
    except OSError as exc:
        raise OperationError(f"无法创建任务产物目录 {job_dir}：{exc}") from exc
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise OperationError(f"任务产物目录不可信，未启动子进程：{job_dir}")
    return job_dir


def _compose_prompt(agent):
    writable = ("、".join(agent.writable_files) if agent.writable_files
                else "未指定（除任务本身要求外不要改动项目文件）")
    return (
        "你是由 delegate-workers 统一接口启动的独立执行 worker。\n"
        f"适配器 agent_id：{agent.agent_id}\n"
        f"工作目录（cwd）：{agent.cwd}\n"
        f"本次任务的临时目录：{agent.job_dir}\n"
        "（TMPDIR/TMP/TEMP 已指向该目录；草稿、日志、下载和测试数据都放在这里，"
        "不要使用系统临时目录。）\n"
        f"沙箱模式：{agent.sandbox}\n"
        f"可写文件边界：{writable}\n\n"
        "工作方式：\n"
        "- 在本条消息给出的授权范围内直接完成并验收任务；主代理保留规划、架构、审查和最终验收。\n"
        "- writable_files 只是指令边界，不是文件系统权限 ACL；实际写权限由沙箱模式决定。\n"
        "- 不要机械地继续委派子代理；如果确有必要继续分工，遵守项目规则并说明理由，委派本身不是被禁止的能力。\n"
        "- 不要修改用户或全局设置，不要安装、发布或提交（任务明确要求时除外）。\n"
        "- 父会话的编辑器、WPS、浏览器等工具不保证被继承；缺少必要工具时请直接说明。\n"
        "- 最终回复请给出：改动文件、执行的命令与结果、以及未验证或不确定的部分。\n\n"
        "--- 任务 ---\n" + agent.task
    )


def _read_output(path):
    """有界读取最终输出：超出上限只读前 N 字节并明确标记截断，绝不整文件读入。"""
    try:
        if not path.is_file():
            return "", {"error": "文件不存在", "truncated": False, "note": None}
        size = path.stat().st_size
        if size == 0:
            return "", {"error": "文件为空", "truncated": False, "note": None}
        with open(path, "rb") as stream:
            data = stream.read(MAX_OUTPUT_BYTES)
        truncated = size > len(data)
        note = None
        if truncated:
            note = (f"最终输出超过 {MAX_OUTPUT_BYTES} 字节上限，这里只返回前 {MAX_OUTPUT_BYTES} 字节；"
                    f"完整输出见 {path}（未作为完整结果回传）。")
        return data.decode("utf-8", "replace"), {"error": None, "truncated": truncated, "note": note}
    except OSError as exc:
        return "", {"error": f"读取失败：{exc}", "truncated": False, "note": None}
