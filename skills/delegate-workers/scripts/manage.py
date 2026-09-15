#!/usr/bin/env python3
"""安装、更新和设置执行子代理，保留当前 Codex 主代理设置。"""

import argparse
import contextlib
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

import workers
import activation
import platform_support
import project_rules

try:
    import interface_setup
except ImportError:  # pragma: no cover - 仅用于缺少新模块的异常安装
    interface_setup = None

# 缺少 interface_setup 时用空元组，避免 except 表达式本身抛出 AttributeError。
INTERFACE_ERRORS = (interface_setup.InterfaceError,) if interface_setup is not None else ()


PROJECT = "delegate-workers"
REPOSITORY = "https://github.com/haobanz/codex-delegate-workers.git"
RECEIPT = ".delegate-workers-install.json"
REQUIRED = {"SKILL.md", "workers.json", "VERSION", "scripts/workers.py", "scripts/manage.py"}
BASE_RUNTIME_REQUIRED = {"scripts/activation.py", "scripts/platform_support.py",
                         "references/default-delegation.md"}
CAPABILITY_RUNTIME_REQUIRED = {"scripts/capabilities.py", "model-capabilities.json"}
PROJECT_RUNTIME_REQUIRED = {"scripts/project_rules.py", "references/project-delegation.md"}
INTERFACE_RUNTIME_REQUIRED = {"scripts/cli_support.py", "scripts/worker_runtime.py",
                              "scripts/mcp_server.py", "scripts/interface_setup.py"}
RUNTIME_REQUIRED = (BASE_RUNTIME_REQUIRED | CAPABILITY_RUNTIME_REQUIRED | PROJECT_RUNTIME_REQUIRED
                    | INTERFACE_RUNTIME_REQUIRED)
INTERFACE_GUIDANCE = "dw interface disable"
INTERFACE_MARKER = "delegate-workers-interface.json"
# helper.state() 可能返回的已知状态；其它取值一律视为“无法确认”。
INTERFACE_STATES = {"registered", "disabled", "absent", "changed", "conflict"}
EFFORT_LABELS = {"low": "低", "medium": "中", "high": "高", "xhigh": "超高",
                 "max": "最大", "ultra": "极限", "minimal": "最低", "none": "关闭"}


class ManagementError(ValueError):
    pass


def required_files_for_candidate(files):
    """Require each newer runtime pair only when the candidate manifest adopts it."""
    required = REQUIRED | BASE_RUNTIME_REQUIRED
    if set(files) & CAPABILITY_RUNTIME_REQUIRED:
        required |= CAPABILITY_RUNTIME_REQUIRED
    if set(files) & PROJECT_RUNTIME_REQUIRED:
        required |= PROJECT_RUNTIME_REQUIRED
    if set(files) & INTERFACE_RUNTIME_REQUIRED:
        required |= INTERFACE_RUNTIME_REQUIRED
    return required


def validate_worker_for_configuration(worker):
    """Validate one new configuration without making compatibility a read barrier."""
    try:
        validator = workers.validate_worker
    except AttributeError as exc:
        raise ManagementError("当前 workers.py 缺少必要的 validate_worker 接口，未保存配置") from exc
    result = validator(worker)
    if not isinstance(result, dict):
        raise workers.ConfigError("模型兼容性预检返回格式无效")
    status = result.get("status")
    if status == "incompatible":
        raise workers.ConfigError(result.get("error") or "执行模型与思考强度不兼容")
    if status not in {"compatible", "unverified"}:
        raise workers.ConfigError("模型兼容性预检返回未知状态")
    return result


def worker_compatibility(worker):
    """Return a diagnostic for one profile, keeping status readable on one failure."""
    validator = getattr(workers, "validate_worker", None)
    if validator is None:
        return {"status": "error", "source": "管理器与 workers.py 接口不匹配",
                "runtime_verified": False,
                "error": "当前 workers.py 缺少必要的 validate_worker 接口"}
    try:
        result = validator(worker)
    except workers.ConfigError as exc:
        return {"status": "incompatible", "source": "静态兼容性预检",
                "runtime_verified": False, "error": str(exc) or "执行模型与思考强度不兼容"}
    except Exception as exc:
        return {"status": "error", "source": "静态兼容性预检",
                "runtime_verified": False,
                "error": str(exc) or f"{type(exc).__name__}：兼容性预检失败"}
    if not isinstance(result, dict):
        return {"status": "error", "source": "静态兼容性预检", "runtime_verified": False,
                "error": "模型兼容性预检返回格式无效"}
    result = copy.deepcopy(result)
    if result.get("status") not in {"compatible", "unverified"}:
        result["status"] = "error"
        result.setdefault("error", "模型兼容性预检返回未知状态")
    result.setdefault("source", "静态兼容性预检")
    # This command performs static checking only; it never verifies runtime identity.
    result["runtime_verified"] = False
    return result


def compatibility_for_config(config):
    return {name: worker_compatibility(profile) for name, profile in config["profiles"].items()}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".delegate-workers-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def inventory(directory):
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ManagementError(f"无法管理符号链接，请先检查此路径：{path}")
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_file() and relative.as_posix() not in {RECEIPT, "workers.json", "workers.json.bak"}:
            result[relative.as_posix()] = file_hash(path)
    return result


def validate_staged_candidate(stage, managed_files):
    python_files = sorted(stage / name for name in managed_files if name.endswith(".py"))
    for path in python_files:
        try:
            validation = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "py_compile", str(path)],
                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagementError(f"新版本 Python 脚本检查失败，已停止更新：{path.relative_to(stage)}") from exc
        if validation.returncode:
            detail = validation.stderr.strip() or validation.stdout.strip()
            raise ManagementError(f"新版本 Python 脚本无效，已停止更新：{path.relative_to(stage)}"
                                  + (f"：{detail}" if detail else ""))

    entries = [(stage / "scripts/manage.py", "新版本管理入口无法启动，已停止更新")]
    if "scripts/mcp_server.py" in set(managed_files):
        # 采用统一接口的候选版本必须能在目录替换前导入 MCP 运行时依赖。
        entries.append((stage / "scripts/mcp_server.py", "新版本 MCP 服务器无法启动，已停止更新"))
    for entry, failure in entries:
        try:
            startup = subprocess.run(
                [sys.executable, "-X", "utf8", str(entry), "--help"],
                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagementError(failure) from exc
        if startup.returncode:
            detail = startup.stderr.strip() or startup.stdout.strip()
            raise ManagementError(failure + (f"：{detail}" if detail else ""))


def load_receipt(directory):
    if directory.is_symlink():
        raise ManagementError(f"技能目录是符号链接，未进行替换：{directory}")
    if not directory.exists():
        return None
    receipt_path = directory / RECEIPT
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ManagementError(f"目录已存在，但不是本工具安装的版本，未进行覆盖：{directory}")
    value = workers.read_json(receipt_path)
    if (not isinstance(value, dict) or value.get("project") != PROJECT
            or value.get("schema_version") != 1 or not isinstance(value.get("files"), dict)):
        raise ManagementError(f"安装记录格式无效：{receipt_path}")
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("version", "revision")):
        raise ManagementError(f"安装记录中的版本或提交编号无效：{receipt_path}")
    if "command_dir" in value and (not isinstance(value["command_dir"], str)
                                   or not Path(value["command_dir"]).is_absolute()):
        raise ManagementError(f"安装记录中的命令目录无效：{receipt_path}")
    if "activation" in value:
        activation.validate_state(value["activation"])
    if "path_entry_added" in value and type(value["path_entry_added"]) is not bool:
        raise ManagementError("安装记录中的 PATH 所有权标记无效")
    if ("interface_ever_enabled" in value
            and type(value["interface_ever_enabled"]) is not bool):
        raise ManagementError("安装记录中的统一接口启用历史标记无效")
    for relative, digest in value["files"].items():
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative
                or relative in {RECEIPT, "workers.json"} or not isinstance(digest, str)):
            raise ManagementError(f"安装记录中的文件路径无效：{relative}")
    return value


class Installation:
    def __init__(self, codex_home, bin_dir=None):
        self.home = Path(codex_home).expanduser().resolve()
        self.skill = self.home / "skills" / PROJECT
        self.launcher = self.home / "bin" / (PROJECT + (".cmd" if platform_support.WINDOWS else ""))
        self.backups = self.home / "delegate-workers-backups"
        receipt = load_receipt(self.skill)
        saved_bin = receipt.get("command_dir") if receipt else None
        user_codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        default_bin = platform_support.default_bin() if self.home == user_codex else self.home / "bin"
        self.manage_user_path = platform_support.WINDOWS and (
            self.home == user_codex or bool(receipt and receipt.get("path_entry_added")))
        self.command_dir = Path(bin_dir or saved_bin or default_bin).expanduser().resolve()
        if saved_bin and self.command_dir != Path(saved_bin):
            raise ManagementError("已安装版本的命令目录不能直接更改，请先卸载再选择新目录")
        # 统一接口默认使用本机 Codex CLI；本属性只便于测试注入等价命令。
        self.interface_command_provider = None

    def launchers(self):
        suffix = ".cmd" if platform_support.WINDOWS else ""
        paths = [self.launcher, self.command_dir / ("dw" + suffix), self.command_dir / (PROJECT + suffix)]
        if platform_support.WINDOWS:
            paths += [directory / "delegate-workers-entry.py" for directory in (self.launcher.parent, self.command_dir)]
        return list(dict.fromkeys(paths))

    def menu_command(self):
        executable = shutil.which("dw")
        target = self.command_dir / ("dw.cmd" if platform_support.WINDOWS else "dw")
        if executable and Path(executable).resolve() == target:
            return "dw"
        if platform_support.WINDOWS:
            return "& '" + str(target).replace("'", "''") + "'"
        return shlex.quote(str(target))

    def print_commands(self):
        print(f"\n打开菜单：{self.menu_command()}")
        print(f"一键更新：{self.menu_command()} update")
        path_dirs = [Path(part).expanduser().resolve() for part in os.get_exec_path() if part]
        if self.command_dir not in path_dirs:
            if platform_support.WINDOWS:
                print("当前 PowerShell 窗口可运行下面一行，或重新打开终端：")
                print("$env:Path = '" + str(self.command_dir).replace("'", "''") + ";' + $env:Path")
            else:
                print("请在 Shell 配置中添加下面一行，让终端能找到短命令：")
                print(f'export PATH={shlex.quote(str(self.command_dir))}:"$PATH"')

    @contextlib.contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            with platform_support.file_lock(self.home / ".delegate-workers.lock"):
                yield
        except BlockingIOError as exc:
            raise ManagementError("另一个安装或设置操作正在运行，请稍后重试") from exc

    def launcher_content(self, path=None):
        if platform_support.WINDOWS:
            if path is not None and path.suffix == ".py":
                return platform_support.windows_python_launcher(self.skill / "scripts/manage.py", self.home)
            return platform_support.windows_batch_launcher()
        command = " ".join(shlex.quote(part) for part in (
            "python3", str(self.skill / "scripts/manage.py"), "--codex-home", str(self.home)))
        return (f'#!/bin/sh\n# Managed by Delegate Workers\nexec {command} "$@"\n').encode()

    def check_launcher(self):
        for launcher in self.launchers():
            if launcher.is_symlink():
                raise ManagementError(f"命令入口是符号链接，未进行覆盖：{launcher}")
            if launcher.exists():
                content = launcher.read_bytes()
                known = [self.launcher_content(launcher)]
                if platform_support.WINDOWS and launcher.suffix == ".cmd":
                    legacy = platform_support.windows_batch_launcher(legacy=True)
                    known.extend((legacy, legacy.replace(b"\r\nexit /b %errorlevel%\r\n", b" & exit /b\r\n")))
                if content not in known:
                    raise ManagementError(f"命令入口被修改过或存在同名程序，未进行覆盖：{launcher}")

    def write_launchers(self):
        for launcher in self.launchers():
            content = self.launcher_content(launcher)
            if not launcher.exists() or launcher.read_bytes() != content:
                atomic_write(launcher, content, 0o755)

    def restore_launchers(self, previous):
        for launcher, contents in previous.items():
            if contents is not None:
                atomic_write(launcher, contents, 0o755)
            elif launcher.is_file() and launcher.read_bytes() == self.launcher_content(launcher):
                launcher.unlink()

    def interface(self):
        """返回统一接口注册助手；缺少新模块时给出明确错误。"""
        if interface_setup is None:
            raise ManagementError("当前安装缺少 scripts/interface_setup.py，无法管理统一接口；"
                                  "请先运行菜单 1 安装或更新本技能")
        return interface_setup.InterfaceSetup(self.home, self.skill,
                                              self.interface_command_provider)

    def interface_server_present(self):
        return (self.skill / "scripts/mcp_server.py").is_file()

    def interface_summary(self):
        """状态展示用的本地摘要：不调用 Codex CLI，也不创建任何文件。"""
        if interface_setup is None:
            marker = self.home / INTERFACE_MARKER
            return {"name": PROJECT, "state": "unavailable",
                    "state_label": "无法确认（缺少统一接口模块）",
                    "server": str(self.skill / "scripts/mcp_server.py"),
                    "server_present": self.interface_server_present(), "entry": None,
                    "marker_path": str(marker), "marker_present": marker.is_file(),
                    "detail": "当前安装缺少 scripts/interface_setup.py；请更新本技能后再管理统一接口"}
        return self.interface().local_summary()

    def interface_ever_enabled(self):
        """本安装是否成功执行过显式启用；停用不会撤销这段历史。"""
        receipt = load_receipt(self.skill)
        return bool(receipt and receipt.get("interface_ever_enabled") is True)

    def remember_interface_enabled(self, receipt=None):
        """在安装记录里持久化启用历史；不写入任何模型或注册内容。"""
        if receipt is None:
            receipt = load_receipt(self.skill)
        if receipt is None or receipt.get("interface_ever_enabled") is True:
            return
        updated = copy.deepcopy(receipt)
        updated["interface_ever_enabled"] = True
        atomic_write(self.skill / RECEIPT, json_bytes(updated))

    def interface_state_if_available(self, helper):
        """CLI 可用时读取真实注册状态；不可用、出错或状态未知时返回 None。"""
        try:
            value = helper.state()
        except INTERFACE_ERRORS + (OSError, ValueError, subprocess.SubprocessError):
            return None
        if not isinstance(value, dict) or value.get("state") not in INTERFACE_STATES:
            return None
        return value

    def guard_interface_target(self, files):
        """更新或回滚到不含统一接口的版本前，拒绝留下已启用的注册。"""
        if set(files) & INTERFACE_RUNTIME_REQUIRED:
            return None
        if not self.interface_server_present():
            return None
        if interface_setup is None:
            # 缺少助手时无法向 CLI 核对；只有存在明确证据才必须拒绝。
            if self.interface_ever_enabled() or (self.home / INTERFACE_MARKER).is_file():
                raise ManagementError(
                    "当前安装缺少 scripts/interface_setup.py，但存在统一接口启用的记录，"
                    "无法确认注册是否已清理，已停止操作。请先恢复该模块，再运行 "
                    + INTERFACE_GUIDANCE)
            return None
        helper = self.interface()
        # CLI 可用时始终核对真实注册；标记缺失不能掩盖指向本安装的条目。
        current = self.interface_state_if_available(helper)
        if current is None:
            # CLI 不可用：只有从未启用过（既无标记也无启用历史）才继续普通离线生命周期。
            if helper.marker_present() or self.interface_ever_enabled():
                raise ManagementError(self.offline_refusal("已停止操作"))
            return None
        if current.get("references_install") and current.get("enabled"):
            manual = (f"若该条目由你修改过，请手工运行 codex mcp remove {PROJECT}"
                      if not current.get("owned") else "")
            raise ManagementError(
                f"统一接口条目 {PROJECT} 已启用并指向本安装"
                f"（{current.get('state_label') or '状态未知'}），而目标版本不含统一接口，"
                f"操作后会留下失效的服务器路径。请先运行 {INTERFACE_GUIDANCE} 再重试"
                + (f"；{manual}" if manual else ""))
        return current

    def release_interface(self):
        """卸载前清理自己的注册；无法确认时给出明确错误。"""
        if interface_setup is None:
            # 缺少助手时无法核对；有证据就拒绝，从未启用过的普通安装仍可卸载。
            if self.interface_ever_enabled() or (self.home / INTERFACE_MARKER).is_file():
                raise ManagementError(
                    "当前安装缺少 scripts/interface_setup.py，但存在统一接口启用的记录，"
                    "无法确认注册是否已清理，已停止卸载。请先恢复该模块，再运行 "
                    + INTERFACE_GUIDANCE + " 后重试")
            return {"result": "unknown_module"}
        helper = self.interface()
        # 服务器脚本还在，CLI 可用就核对真实注册状态，即使标记已缺失。
        current = self.interface_state_if_available(helper)
        if current is None:
            # CLI 不可用：只有从未启用过（既无标记也无启用历史）才继续普通离线卸载。
            if helper.marker_present() or self.interface_ever_enabled():
                raise ManagementError(self.offline_refusal("已停止卸载"))
            return {"result": "unavailable_not_registered"}
        return self.release_owned_interface(current)

    def offline_refusal(self, action):
        """无法核对时说明启用历史，不把删除标记当作绕过清理的办法。"""
        helper = self.interface()
        if self.interface_ever_enabled() and not helper.marker_present():
            reason = ("安装记录显示统一接口曾被启用，但标记文件已缺失且本地 Codex CLI 不可用，"
                      "无法确认注册是否已清理")
        else:
            reason = "无法确认统一接口条目已清理（本地 Codex CLI 或标记文件不可用）"
        return (f"{reason}，{action}。请先恢复本地 Codex CLI，再运行 {INTERFACE_GUIDANCE} 后重试；"
                "不要通过删除标记文件绕过清理")

    def release_owned_interface(self, current):
        """按真实注册状态清理：本工具拥有的条目会被移除，其余保持原样。"""
        helper = self.interface()
        state = current.get("state")
        if state == "absent":
            helper.remove_marker()
            return {"result": "absent"}
        if current.get("owned"):
            # owned 表示条目内容等于本安装的注册：用助手自身的停用路径安全删除。
            try:
                helper.disable()
            except INTERFACE_ERRORS as exc:
                raise ManagementError(f"无法删除本工具的 MCP 条目，已停止卸载：{exc}。"
                                      f"请先运行 {INTERFACE_GUIDANCE} 或手工运行 "
                                      f"codex mcp remove {PROJECT} 再卸载") from exc
            return {"result": "removed"}
        if not current.get("references_install"):
            # 与本安装无关的同名条目（conflict）才保留；其它未知状态一律拒绝。
            if state == "conflict":
                helper.remove_marker()
                return {"result": "foreign_preserved", "detail": current.get("detail")}
            raise ManagementError(
                "无法确认统一接口条目状态，已停止卸载。请先运行 "
                + INTERFACE_GUIDANCE + " 后重试")
        # 内容被改过但指向本安装：不删除（可能带着你自己的修改），一律拒绝卸载。
        if current.get("enabled"):
            reason = "已启用"
        else:
            reason = "当前禁用，但指向本安装的引用会在卸载后失效"
        raise ManagementError(
            f"统一接口条目 {PROJECT} 指向本安装，但内容与本工具记录的注册不同（{reason}）；"
            f"已停止卸载并保留该条目。请先运行 {INTERFACE_GUIDANCE}；"
            f"若该条目由你修改过，请手工运行 codex mcp remove {PROJECT} 确认删除后再卸载")

    def interface_enable(self):
        with self.locked():
            try:
                result = self.interface().enable()
            except INTERFACE_ERRORS as exc:
                raise ManagementError(f"启用统一接口失败：{exc}") from exc
            if result.get("result") in {"enabled", "already_enabled"}:
                try:
                    self.remember_interface_enabled()
                except (OSError, ManagementError) as exc:
                    raise ManagementError(
                        "统一接口已启用，但安装记录中的启用历史未能保存；"
                        "后续无法确认注册是否清理。请检查技能目录后重试：" + str(exc)) from exc
            return result

    def interface_disable(self):
        with self.locked():
            try:
                return self.interface().disable()
            except INTERFACE_ERRORS as exc:
                raise ManagementError(f"停用统一接口失败：{exc}") from exc

    def interface_status(self):
        return self.interface().status()

    def config(self):
        if load_receipt(self.skill) is None:
            raise ManagementError("请先选择菜单 1 安装本技能")
        return workers.validate_config(workers.read_json(self.skill / "workers.json"))

    def changed_files(self, receipt):
        actual = inventory(self.skill)
        return [name for name, digest in receipt["files"].items() if actual.get(name) != digest]

    def backup_path(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
        return self.backups / name

    @contextlib.contextmanager
    def user_path_transaction(self, receipt, *, remove=False):
        if not self.manage_user_path:
            yield False
            return
        before = platform_support.read_user_path()
        value, value_type = before if before is not None else ("", 2)
        owned = bool(receipt and receipt.get("path_entry_added"))
        updated = platform_support.update_path(value, self.command_dir, remove=remove) if not remove or owned else value
        after = (updated, value_type)
        changed = updated != value
        if changed:
            platform_support.write_user_path(after)
        try:
            yield False if remove else owned or changed
        except BaseException:
            if changed and platform_support.read_user_path() == after:
                platform_support.write_user_path(before)
            raise

    @contextlib.contextmanager
    def rules_transaction(self, edits):
        changed = []
        try:
            for path, (before, after) in edits.items():
                if activation.read_file(path) != before:
                    raise ManagementError(f"指令文件在操作期间发生变化，请重试：{path}")
                if before is not None:
                    backup = self.backup_path()
                    backup.mkdir()
                    atomic_write(backup / path.name, before)
                mode = path.stat().st_mode & 0o777 if before is not None else 0o644
                if after is None:
                    path.unlink()
                else:
                    atomic_write(path, after, mode)
                changed.append((path, before, mode))
            yield
        except BaseException:
            for path, before, mode in reversed(changed):
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, before, mode)
            raise

    def apply(self, source, revision="local", source_files=None):
        """Stage and validate the candidate before swapping the installed directory."""
        source = Path(source).resolve()
        present = inventory(source)
        files = present if source_files is None else source_files
        required_files = required_files_for_candidate(files)
        for relative in required_files:
            if not (source / relative).is_file():
                raise ManagementError(f"新版本缺少必要文件：{relative}")
        if not (required_files - {"workers.json"}) <= files.keys():
            raise ManagementError("版本文件清单缺少必要文件")
        for relative, digest in files.items():
            if present.get(relative) != digest:
                raise ManagementError(f"版本文件校验失败：{relative}")
        version = (source / "VERSION").read_text(encoding="utf-8").strip()
        if not version or len(version) > 64:
            raise ManagementError("版本号无效")
        old = load_receipt(self.skill)
        previous_activation = old.get("activation") if old else None
        requested_mode = (previous_activation or {"mode": "auto"})["mode"]
        # 恢复已卸载的备份（回滚）时，启用历史也随备份的安装记录恢复。
        restored_history = False
        if old is None and (source / RECEIPT).is_file():
            restored_receipt = load_receipt(source)
            requested_mode = (restored_receipt.get("activation") or {"mode": "auto"})["mode"]
            restored_history = restored_receipt.get("interface_ever_enabled") is True
        template_path = source / "references/default-delegation.md"
        template = template_path.read_text(encoding="utf-8")
        activation_state, rule_edits = activation.plan(
            self.home, self.skill, previous_activation,
            requested_mode, template)
        self.check_launcher()
        if old:
            # 目标版本不含统一接口时，已注册的条目会指向被替换掉的服务器路径。
            self.guard_interface_target(files)
        if old:
            changes = self.changed_files(old)
            if changes:
                raise ManagementError("已安装代码存在本地修改，请先保存这些修改再更新："
                                      + ", ".join(changes))
            for relative in files.keys() - old["files"].keys():
                if (self.skill / relative).exists():
                    raise ManagementError(f"新版本与个人文件冲突，未进行覆盖：{relative}")
        self.skill.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".delegate-workers-stage-", dir=self.skill.parent))
        backup = None
        old_launchers = {path: path.read_bytes() if path.exists() else None for path in self.launchers()}
        try:
            if old:
                shutil.copytree(self.skill, stage, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
                for relative in old["files"].keys() - files.keys():
                    (stage / relative).unlink()
            for relative in files:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, destination)
            if not old:
                shutil.copyfile(source / "workers.json", stage / "workers.json")
            validation = subprocess.run(
                [sys.executable, "-X", "utf8", str(stage / "scripts/workers.py"), "show"],
                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
            if validation.returncode:
                raise ManagementError("新版本无法读取当前执行配置，已停止更新：" + validation.stderr.strip())
            try:
                migrated = workers.validate_config(json.loads(validation.stdout))
            except json.JSONDecodeError as exc:
                raise ManagementError("新版本返回的模型配置无效，已停止更新") from exc
            config_path = stage / "workers.json"
            config_changed = migrated != workers.read_json(config_path)
            if config_changed:
                atomic_write(stage / "workers.json.bak", config_path.read_bytes())
                atomic_write(config_path, json_bytes(migrated))
            validate_staged_candidate(stage, files)
            receipt = {"schema_version": 1, "project": PROJECT, "repository": REPOSITORY,
                       "version": version, "revision": revision, "files": files,
                       "command_dir": str(self.command_dir), "activation": activation_state}
            # 启用历史跨更新/回滚保留；它不含模型设置，也不代表当前注册状态。
            if (old and old.get("interface_ever_enabled") is True) or restored_history:
                receipt["interface_ever_enabled"] = True
            with self.user_path_transaction(old) as path_owned:
                if platform_support.WINDOWS:
                    receipt["path_entry_added"] = path_owned
                atomic_write(stage / RECEIPT, json_bytes(receipt))
                if old and old == receipt and not config_changed:
                    self.write_launchers()
                    return {"result": "unchanged", "version": version, "revision": revision}
                with self.rules_transaction(rule_edits):
                    self.write_launchers()
                    if old:
                        backup = self.backup_path()
                        self.skill.rename(backup)
                    try:
                        stage.rename(self.skill)
                    except BaseException:
                        if backup is not None:
                            backup.rename(self.skill)
                        raise
            return {"result": "updated" if old else "installed", "version": version,
                    "revision": revision, "backup": str(backup) if backup else None}
        except BaseException:
            self.restore_launchers(old_launchers)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def install(self, source=None, require_existing=False):
        with self.locked():
            if require_existing and load_receipt(self.skill) is None:
                raise ManagementError("尚未安装，请先选择菜单 1 安装本技能")
            if source is not None:
                root = Path(source).expanduser().resolve()
                return self.apply(root / "skills" / PROJECT, revision_of(root))
            with tempfile.TemporaryDirectory(prefix="delegate-workers-download-",
                                             dir=platform_support.project_tmp()) as temporary:
                root = Path(temporary) / "repository"
                run_git(["clone", "--quiet", "--depth", "1", "--branch", "main", "--",
                         REPOSITORY, str(root)])
                return self.apply(root / "skills" / PROJECT, revision_of(root))

    def save_config(self, config):
        config = workers.validate_config(config)
        previous = self.skill / "workers.json"
        atomic_write(self.skill / "workers.json.bak", previous.read_bytes())
        atomic_write(previous, json_bytes(config))

    def configure(self, profile, model=None, effort=None, default=False):
        with self.locked():
            config = copy.deepcopy(self.config())
            current = config["profiles"].get(profile, {})
            candidate = dict(current)
            if model is not None:
                if effort is None:
                    raise ManagementError("更改模型时必须同时指定思考强度（--effort）")
                candidate["model"] = model
            if effort is not None:
                candidate["reasoning_effort"] = effort
            config["profiles"][profile] = candidate
            if default:
                config["default_profile"] = profile
            normalized = workers.validate_config(config)
            validate_worker_for_configuration(normalized["profiles"][profile])
            self.save_config(normalized)
            return normalized

    def set_mode(self, mode):
        with self.locked():
            receipt = load_receipt(self.skill)
            if receipt is None:
                raise ManagementError("请先安装本技能")
            template_path = self.skill / "references/default-delegation.md"
            template = template_path.read_text(encoding="utf-8") if template_path.is_file() else None
            state, edits = activation.plan(self.home, self.skill, receipt.get("activation"), mode,
                                           template, repair=True)
            receipt["activation"] = state
            with self.rules_transaction(edits):
                atomic_write(self.skill / RECEIPT, json_bytes(receipt))
            return {"activation": activation.status(self.home, state)}

    def status(self):
        receipt = load_receipt(self.skill)
        if receipt is None:
            return {"installed": False, "skill": str(self.skill)}
        config = self.config()
        return {"installed": True, "version": receipt["version"], "revision": receipt["revision"],
                "skill": str(self.skill), "launcher": str(self.launcher),
                "command_dir": str(self.command_dir), "menu_command": self.menu_command(),
                "local_code_changes": self.changed_files(receipt), "config": config,
                "compatibility": compatibility_for_config(config),
                "activation": activation.status(self.home, receipt.get("activation")),
                "interface": self.interface_summary(),
                "main_session": "unchanged"}

    def project_init(self, path=None, profile=None, model=None, effort=None):
        explicit = profile is not None or model is not None or effort is not None
        if explicit:
            # Validate option shape before creating the project lock. The final
            # pair is resolved under that lock so an existing project snapshot
            # cannot be replaced by the current global default accidentally.
            if profile is not None and (not isinstance(profile, str)
                                        or not workers.PROFILE_NAME.fullmatch(profile)):
                raise workers.ConfigError("预设名称无效")
            if model is not None:
                workers.check_model(model)
                if effort is None:
                    raise workers.ConfigError("更改模型时请同时指定思考强度 --effort")
            if effort is not None:
                workers.check_effort(effort)

            def selection_resolver(state):
                config = self.config()
                if state is not None and profile is None:
                    candidate = dict(state["worker"])
                    if model is not None:
                        candidate["model"] = model
                    if effort is not None:
                        candidate["reasoning_effort"] = effort
                    return state["selection"]["profile"], candidate
                resolved = workers.resolve(config, profile=profile, model=model, effort=effort)
                return resolved["profile"], resolved["worker_request"]

            return project_rules.init_project(
                path, template_path=self.skill / "references/project-delegation.md",
                selection_resolver=selection_resolver)

        def default_selection():
            config = self.config()
            resolved = workers.resolve(config)
            return resolved["profile"], resolved["worker_request"]

        return project_rules.init_project(
            path, template_path=self.skill / "references/project-delegation.md",
            default_selection=default_selection)

    def project_sync(self, path=None):
        return project_rules.sync_project(
            path, template_path=self.skill / "references/project-delegation.md")

    def project_status(self, path=None):
        return project_rules.status_project(path)

    def project_disable(self, path=None):
        return project_rules.disable_project(path)

    def rollback(self):
        with self.locked():
            backups = sorted(self.backups.glob("*"), reverse=True)
            previous = next((path for path in backups if path.is_dir() and (path / RECEIPT).is_file()), None)
            if previous is None:
                raise ManagementError("没有可用于回滚的安装备份")
            receipt = load_receipt(previous)
            return self.apply(previous, receipt["revision"], receipt["files"])

    def uninstall(self):
        with self.locked():
            receipt = load_receipt(self.skill)
            if receipt is None:
                return {"result": "not_installed"}
            _, rule_edits = activation.plan(self.home, self.skill, receipt.get("activation"), "on-demand",
                                            repair=True)
            self.check_launcher()
            # 先清理自己的 MCP 条目，避免卸载后留下指向已删除脚本的注册。
            released = self.release_interface()
            old_launchers = {path: path.read_bytes() if path.exists() else None for path in self.launchers()}
            backup = self.backup_path()
            moved = False
            restored = False
            try:
                with self.user_path_transaction(receipt, remove=True), self.rules_transaction(rule_edits):
                    self.skill.rename(backup)
                    moved = True
                    try:
                        for launcher in self.launchers():
                            if launcher.exists():
                                launcher.unlink()
                    except BaseException:
                        backup.rename(self.skill)
                        moved = False
                        self.restore_launchers(old_launchers)
                        restored = True
                        raise
            except BaseException as exc:
                if released.get("result") == "removed":
                    # 注册已删除且不会自动恢复；代码与命令入口仍按既有逻辑还原，
                    # 因此这里只报告“部分完成”，不声称全部回退。
                    if restored or (not moved and self.skill.is_dir()):
                        outcome = "本工具的代码和命令入口保持原样或已恢复"
                    else:
                        outcome = "本工具的代码和命令入口未能确认恢复，请检查后再重试"
                    raise ManagementError(
                        "卸载未完成：统一接口条目已删除，且不会自动恢复；" + outcome
                        + "。如需继续使用统一接口，请在重新安装后运行 dw interface enable") from exc
                raise
            return {"result": "uninstalled", "backup": str(backup)}


def run_git(arguments):
    try:
        result = subprocess.run(["git", *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagementError(f"Git 操作失败，请检查网络和 Git 是否可用：{exc}") from exc
    if result.returncode:
        raise ManagementError("Git 操作失败：" + (result.stderr.strip() or "请检查网络和仓库访问权限"))
    return result.stdout.strip()


def revision_of(root):
    if not (root / ".git").exists():
        return "local"
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode == 1:
        return "local"
    if result.returncode:
        raise ManagementError("无法读取源码提交编号：" + result.stderr.strip())
    return result.stdout.strip()


def profile_label(name):
    return {"default": "常用模型（default）", "complex": "备选模型（complex）"}.get(name, name)


def print_config(config):
    print(f"默认执行预设：{profile_label(config['default_profile'])}")
    for name, profile in config["profiles"].items():
        effort = profile["reasoning_effort"]
        print(f"\n  {profile_label(name)}")
        print(f"    模型：{profile['model']}")
        print(f"    思考强度：{EFFORT_LABELS.get(effort, effort)}（{effort}）")


def print_compatibility(compatibility):
    print("\n模型兼容性预检：")
    labels = {"compatible": "兼容", "unverified": "未验证", "incompatible": "不兼容",
              "error": "检查错误"}
    for name, result in compatibility.items():
        status = result.get("status", "error") if isinstance(result, dict) else "error"
        label = labels.get(status, status)
        details = []
        if isinstance(result, dict) and result.get("source"):
            details.append(f"来源：{result['source']}")
        if isinstance(result, dict) and result.get("error"):
            details.append(f"错误：{result['error']}")
        if not isinstance(result, dict) or not result.get("runtime_verified", False):
            details.append("运行时身份：未独立验证")
        print(f"  {profile_label(name)}：{label}" + ("；" + "；".join(details) if details else ""))


def print_activation(state):
    print(f"默认委派：{'开启' if state['mode'] == 'auto' else '关闭（按需匹配）'}")
    if state.get("file"):
        print(f"启动规则：{state['file']}")
    if state.get("issue"):
        print(f"规则检查：{state['issue']}")
    elif state["mode"] == "auto":
        print("规则检查：已写入，新 Codex 会话加载后生效")


def print_project_status(value):
    print(f"项目根目录：{value['project_root']}")
    print(f"项目状态：{value['status']}")
    print(f"状态文件：{value['state_file']}（{'存在' if value['state_present'] else '不存在'}）")
    print(f"有效指令：{value['effective_file']}；文件存在：{'是' if value['file_present'] else '否'}")
    print(f"完整性：{value['integrity']}")
    if value.get("worker"):
        worker = value["worker"]
        print(f"项目执行模型：{worker['model']} / {worker['reasoning_effort']}")
    for diagnostic in value.get("diagnostics", []):
        print(f"诊断：{diagnostic}")
    session_loaded = value.get("session_loaded")
    session_label = "未验证" if session_loaded is None else ("是" if session_loaded else "否")
    print(f"会话加载：{session_label}；运行时身份已验证：{value.get('runtime_verified', False)}")
    print(f"操作建议：{value.get('action', '')}")


def print_project_change(value):
    labels = {"initialized": "项目委派已启用", "reenabled": "项目委派已重新启用",
              "updated": "项目委派已更新", "synced": "项目委派已同步",
              "disabled": "项目委派已禁用", "unchanged": "项目委派无需变化"}
    print(labels.get(value.get("result"), value.get("result", "完成")))
    print(f"项目根目录：{value.get('project_root')}")
    if value.get("worker"):
        worker = value["worker"]
        print(f"项目执行模型：{worker['model']} / {worker['reasoning_effort']}")
    compatibility = value.get("compatibility")
    if isinstance(compatibility, dict):
        if compatibility.get("warning"):
            print(f"兼容性警告：{compatibility['warning']}")
        if compatibility.get("error"):
            print(f"兼容性诊断：{compatibility['error']}")
    if value.get("backup"):
        print(f"备份位置：{value['backup']}")
    print("请重新启动或重新读取 Codex 任务以加载项目指令；当前工具不能确认活动会话已加载。")


def print_interface(value):
    labels = {"registered": "已启用", "disabled": "已禁用（条目仍在配置中）", "absent": "未注册",
              "changed": "内容与原始注册不一致，未改动", "conflict": "同名条目属于其他配置，未改动",
              "unavailable": "无法确认"}
    print(f"统一接口条目：{value.get('name', PROJECT)}")
    print(f"状态：{labels.get(value.get('state'), value.get('state_label') or value.get('state') or '未知')}")
    if value.get("detail"):
        print(f"说明：{value['detail']}")
    print(f"服务器脚本：{value.get('server')}（{'存在' if value.get('server_present') else '不存在'}）")
    if value.get("marker_path"):
        print(f"所有权标记：{value['marker_path']}（{'存在' if value.get('marker_present') else '不存在'}）")
    entry = value.get("entry")
    if isinstance(entry, dict):
        print(f"条目内容：{entry.get('transport_type')} / {entry.get('command')}"
              + (f" / {' '.join(entry['args'])}" if entry.get("args") else ""))
    expected = value.get("expected")
    if isinstance(expected, dict):
        print(f"本安装期望：{expected.get('command')} / {' '.join(expected.get('args', []))}")
    if value.get("restart_required"):
        print("请重新启动 Codex 会话以加载统一接口；当前会话不会自动启用它。")
    print("主代理模型和思考强度：沿用当前 Codex 会话设置；本操作未改动 workers.json。")


def print_result(value, *, human=False):
    if not human:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    if value.get("project") == "interface":
        print_interface(value["interface"])
    elif value.get("project") == "status":
        print_project_status(value)
    elif value.get("project") in {"init", "sync", "disable"}:
        print_project_change(value)
    elif "installed" in value:
        print(f"\n安装状态：{'已安装' if value['installed'] else '未安装'}")
        print(f"技能目录：{value['skill']}")
        if not value["installed"]:
            return
        print(f"当前版本：{value['version']}")
        print_activation(value["activation"])
        print(f"提交编号：{value['revision']}")
        changes = value["local_code_changes"]
        print(f"代码检查：{'存在本地修改：' + ', '.join(changes) if changes else '正常'}")
        print_config(value["config"])
        print_compatibility(value.get("compatibility", {}))
        interface = value.get("interface")
        if isinstance(interface, dict):
            print("\n统一接口（本机摘要）：")
            print(f"  服务器脚本：{interface.get('server')}"
                  f"（{'存在' if interface.get('server_present') else '不存在'}）")
            print(f"  所有权标记：{'存在' if interface.get('marker_present') else '不存在'}")
            print(f"  核对方式：{interface.get('detail', '')}")
        print("\n主代理模型和思考强度：沿用当前 Codex 会话设置")
    elif "activation" in value:
        print_activation(value["activation"])
        print("请重新启动 Codex 会话以加载新的启动规则。")
    elif "profiles" in value:
        print_config(value)
        print("\n执行配置已保存。")
    elif "result" in value:
        labels = {"installed": "安装完成", "updated": "版本已更新", "unchanged": "已是当前版本",
                  "uninstalled": "已卸载，备份已保留", "not_installed": "尚未安装"}
        print(labels.get(value["result"], value["result"]))
        if value.get("version"):
            print(f"版本：{value['version']}")
        if value.get("backup"):
            print(f"备份位置：{value['backup']}")


def select_effort(stream, current):
    options = list(EFFORT_LABELS)
    print("思考强度：")
    for index, effort in enumerate(options, 1):
        print(f"  {index}. {EFFORT_LABELS[effort]}（{effort}）")
    default = str(options.index(current) + 1)
    choice = prompt(stream, "选择强度编号，也可输入中文或英文档位", default)
    if choice in {str(index) for index in range(1, len(options) + 1)}:
        return options[int(choice) - 1]
    return {label: effort for effort, label in EFFORT_LABELS.items()}.get(choice, choice)


def prompt(stream, label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    print(f"{label}{suffix}: ", end="", flush=True)
    line = stream.readline()
    if not line:
        raise EOFError
    return line.strip() or default or ""


def menu(installation, source=None, stream=None):
    if stream is None:
        try:
            with open("CONIN$" if platform_support.WINDOWS else "/dev/tty", "r", encoding="utf-8") as terminal:
                return menu(installation, source, terminal)
        except OSError as exc:
            if sys.stdin.isatty():
                return menu(installation, source, sys.stdin)
            raise ManagementError("菜单需要交互式终端；脚本中请使用 install、update、configure 或 status 命令") from exc
    while True:
        receipt = load_receipt(installation.skill)
        if receipt:
            print_activation(activation.status(installation.home, receipt.get("activation")))
        print("\n执行子代理管理\n"
              "1. 安装 / 更新\n2. 设置执行模型和思考强度\n3. 查看状态和当前设置\n"
              "4. 开启 / 关闭默认委派\n5. 回滚版本（保留执行设置）\n6. 卸载\n"
              "7. 统一接口（启用 / 停用 / 查看注册状态）\n0. 退出")
        try:
            choice = prompt(stream, "请选择", "0")
            if choice == "0":
                return
            if choice == "1":
                print_result(installation.install(source), human=True)
                print("请新建 Codex 任务以加载更新后的技能。")
                installation.print_commands()
                return
            elif choice == "2":
                config = installation.config()
                for name, profile in config["profiles"].items():
                    print(f"  {profile_label(name)}：{profile['model']} / {EFFORT_LABELS[profile['reasoning_effort']]}")
                name = prompt(stream, "模型预设名称（可输入已有名称或新名称）", config["default_profile"])
                current = config["profiles"].get(name, {})
                model = prompt(stream, "执行模型 ID", current.get("model"))
                effort = select_effort(stream, current.get("reasoning_effort", "medium"))
                default = prompt(stream, "设为默认执行预设？是/否", "否").lower() in {"是", "y", "yes"}
                installation.configure(name, model, effort, default=default)
                print("执行配置已保存，主代理设置未改变。")
            elif choice == "3":
                print_result(installation.status(), human=True)
            elif choice == "5":
                print_result(installation.rollback(), human=True)
                print("请新建 Codex 任务以重新加载技能。")
                return
            elif choice == "6":
                if prompt(stream, "输入“卸载”确认移除本技能，其他输入取消") in {"卸载", "uninstall"}:
                    print_result(installation.uninstall(), human=True)
                    return
            elif choice == "4":
                current = installation.status()
                if not current["installed"]:
                    raise ManagementError("请先选择菜单 1 安装本技能")
                print_activation(current["activation"])
                answer = prompt(stream, "默认委派：1 开启，2 关闭", "1" if current["activation"]["mode"] == "auto" else "2")
                if answer not in {"1", "2"}:
                    raise ManagementError("请输入 1 或 2")
                print_result(installation.set_mode("auto" if answer == "1" else "on-demand"), human=True)
            elif choice == "7":
                if receipt is None:
                    raise ManagementError("请先选择菜单 1 安装本技能")
                status = installation.interface_status()
                print_result(status, human=True)
                answer = prompt(stream, "统一接口：1 启用，2 停用，3 仅查看状态", "3")
                if answer == "1":
                    print_result(installation.interface_enable(), human=True)
                elif answer == "2":
                    if prompt(stream, "输入“停用”确认移除本工具注册的统一接口条目") in {"停用", "disable"}:
                        print_result(installation.interface_disable(), human=True)
                elif answer != "3":
                    raise ManagementError("请输入 1、2 或 3")
            else:
                print("请输入 0 到 7 的菜单编号。")
        except EOFError:
            return
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"操作失败：{exc}", file=sys.stderr)


def main(argv=None):
    platform_support.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--source", type=Path, help="使用本地源码目录进行安装")
    parser.add_argument("--bin-dir", type=Path, help="dw 和 delegate-workers 命令的安装目录")
    commands = parser.add_subparsers(dest="command")
    for command in ("install", "update", "menu", "status", "rollback"):
        commands.add_parser(command)
    configure = commands.add_parser("configure")
    configure.add_argument("--profile", required=True)
    configure.add_argument("--model")
    configure.add_argument("--effort")
    configure.add_argument("--default", action="store_true")
    mode = commands.add_parser("mode", help="开启或关闭默认委派")
    mode.add_argument("value", choices=["auto", "on-demand"])
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    interface = commands.add_parser("interface", help="管理统一 worker 接口的 MCP 注册")
    interface_commands = interface.add_subparsers(dest="interface_command", required=True)
    interface_commands.add_parser("enable", help="在 Codex 配置中注册本工具的统一接口条目")
    interface_commands.add_parser("disable", help="移除本工具注册的统一接口条目")
    interface_commands.add_parser("status", help="只读查看统一接口注册状态")
    project = commands.add_parser("project", help="管理当前项目的委派规则")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_init = project_commands.add_parser("init", help="启用或重新配置项目委派")
    project_init.add_argument("--path", type=Path)
    project_init.add_argument("--profile")
    project_init.add_argument("--model")
    project_init.add_argument("--effort")
    project_sync = project_commands.add_parser("sync", help="使用项目快照同步委派规则")
    project_sync.add_argument("--path", type=Path)
    project_status = project_commands.add_parser("status", help="查看项目委派状态（只读）")
    project_status.add_argument("--path", type=Path)
    project_disable = project_commands.add_parser("disable", help="禁用项目委派规则")
    project_disable.add_argument("--path", type=Path)
    args = parser.parse_args(argv)
    try:
        installation = Installation(args.codex_home, args.bin_dir)
        if args.command is None or args.command == "menu":
            menu(installation, args.source)
            return 0
        if args.command in {"install", "update"}:
            output = installation.install(args.source, require_existing=args.command == "update")
        elif args.command == "project":
            if args.project_command == "init":
                output = installation.project_init(args.path, args.profile, args.model, args.effort)
            elif args.project_command == "sync":
                output = installation.project_sync(args.path)
            elif args.project_command == "status":
                output = installation.project_status(args.path)
            else:
                output = installation.project_disable(args.path)
        elif args.command == "configure":
            output = installation.configure(args.profile, args.model, args.effort, default=args.default)
        elif args.command == "mode":
            output = installation.set_mode(args.value)
        elif args.command == "rollback":
            output = installation.rollback()
        elif args.command == "uninstall":
            if not args.yes:
                raise ManagementError("请使用 uninstall --yes，或在菜单中选择“卸载”")
            output = installation.uninstall()
        elif args.command == "interface":
            if args.interface_command == "enable" and load_receipt(installation.skill) is None:
                raise ManagementError("请先选择菜单 1 安装本技能")
            output = {"enable": installation.interface_enable,
                      "disable": installation.interface_disable,
                      "status": installation.interface_status}[args.interface_command]()
        else:
            output = installation.status()
        print_result(output, human=sys.stdout.isatty())
        if args.command in {"install", "update", "rollback"}:
            installation.print_commands()
            print("请重新启动 Codex 会话以加载技能及默认委派规则。")
        elif args.command == "interface" and args.interface_command in {"enable", "disable"}:
            print("请重新启动 Codex 会话，使统一接口注册的改动生效；"
                  "当前会话仍按启动时的配置运行。")
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
