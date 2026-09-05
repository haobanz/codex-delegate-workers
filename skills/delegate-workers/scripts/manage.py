#!/usr/bin/env python3
"""安装、更新和设置执行子代理，保留当前 Codex 主代理设置。"""

import argparse
import contextlib
import copy
import fcntl
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


PROJECT = "delegate-workers"
REPOSITORY = "https://github.com/haobanz/codex-delegate-workers.git"
RECEIPT = ".delegate-workers-install.json"
REQUIRED = {"SKILL.md", "workers.json", "VERSION", "scripts/workers.py", "scripts/manage.py"}
EFFORT_LABELS = {"low": "低", "medium": "中", "high": "高", "xhigh": "超高",
                 "max": "最大", "ultra": "极限", "minimal": "最低", "none": "关闭"}


class ManagementError(ValueError):
    pass


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
    for relative, digest in value["files"].items():
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute() or ".." in path.parts or "\\" in relative
                or relative in {RECEIPT, "workers.json"} or not isinstance(digest, str)):
            raise ManagementError(f"安装记录中的文件路径无效：{relative}")
    return value


class Installation:
    def __init__(self, codex_home, bin_dir=None):
        self.home = Path(codex_home).expanduser().resolve()
        self.skill = self.home / "skills" / PROJECT
        self.launcher = self.home / "bin" / PROJECT
        self.backups = self.home / "delegate-workers-backups"
        receipt = load_receipt(self.skill)
        saved_bin = receipt.get("command_dir") if receipt else None
        user_codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        default_bin = Path.home() / ".local/bin" if self.home == user_codex else self.home / "bin"
        self.command_dir = Path(bin_dir or saved_bin or default_bin).expanduser().resolve()
        if saved_bin and self.command_dir != Path(saved_bin):
            raise ManagementError("已安装版本的命令目录不能直接更改，请先卸载再选择新目录")

    def launchers(self):
        return list(dict.fromkeys((self.launcher, self.command_dir / "dw", self.command_dir / PROJECT)))

    def menu_command(self):
        executable = shutil.which("dw")
        if executable and Path(executable).resolve() == self.command_dir / "dw":
            return "dw"
        return shlex.quote(str(self.command_dir / "dw"))

    def print_commands(self):
        print(f"\n打开菜单：{self.menu_command()}")
        print(f"一键更新：{self.menu_command()} update")
        path_dirs = [Path(part).expanduser().resolve() for part in os.get_exec_path() if part]
        if self.command_dir not in path_dirs:
            print("请在 Shell 配置中添加下面一行，让终端能找到短命令：")
            print(f'export PATH={shlex.quote(str(self.command_dir))}:"$PATH"')

    @contextlib.contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with (self.home / ".delegate-workers.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ManagementError("另一个安装或设置操作正在运行，请稍后重试") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def launcher_content(self):
        command = " ".join(shlex.quote(part) for part in (
            "python3", str(self.skill / "scripts/manage.py"), "--codex-home", str(self.home)))
        return (f'#!/bin/sh\n# Managed by Delegate Workers\nexec {command} "$@"\n').encode()

    def check_launcher(self):
        for launcher in self.launchers():
            if launcher.is_symlink():
                raise ManagementError(f"命令入口是符号链接，未进行覆盖：{launcher}")
            if launcher.exists() and launcher.read_bytes() != self.launcher_content():
                raise ManagementError(f"命令入口被修改过或存在同名程序，未进行覆盖：{launcher}")

    def write_launchers(self):
        for launcher in self.launchers():
            atomic_write(launcher, self.launcher_content(), 0o755)

    def restore_launchers(self, previous):
        for launcher, contents in previous.items():
            if contents is not None:
                atomic_write(launcher, contents, 0o755)
            elif launcher.is_file() and launcher.read_bytes() == self.launcher_content():
                launcher.unlink()

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
        for relative in REQUIRED:
            if not (source / relative).is_file():
                raise ManagementError(f"新版本缺少必要文件：{relative}")
        if not (REQUIRED - {"workers.json"}) <= files.keys():
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
        if old is None and (source / RECEIPT).is_file():
            restored_receipt = load_receipt(source)
            requested_mode = (restored_receipt.get("activation") or {"mode": "auto"})["mode"]
        template_path = source / "references/default-delegation.md"
        template = template_path.read_text(encoding="utf-8") if template_path.is_file() else None
        activation_state, rule_edits = activation.plan(
            self.home, self.skill, previous_activation,
            requested_mode if template is not None else "on-demand", template)
        self.check_launcher()
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
                [sys.executable, str(stage / "scripts/workers.py"), "validate"],
                capture_output=True, text=True, check=False, timeout=30)
            if validation.returncode:
                raise ManagementError("新版本无法读取当前执行配置，已停止更新：" + validation.stderr.strip())
            receipt = {"schema_version": 1, "project": PROJECT, "repository": REPOSITORY,
                       "version": version, "revision": revision, "files": files,
                       "command_dir": str(self.command_dir), "activation": activation_state}
            atomic_write(stage / RECEIPT, json_bytes(receipt))
            if old and old == receipt:
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
            with tempfile.TemporaryDirectory(prefix="delegate-workers-download-") as temporary:
                root = Path(temporary) / "repository"
                run_git(["clone", "--quiet", "--depth", "1", "--branch", "main", "--",
                         REPOSITORY, str(root)])
                return self.apply(root / "skills" / PROJECT, revision_of(root))

    def save_config(self, config):
        workers.validate_config(config)
        previous = self.skill / "workers.json"
        atomic_write(self.skill / "workers.json.bak", previous.read_bytes())
        atomic_write(previous, json_bytes(config))

    def configure(self, profile, model=None, effort=None, fallback=None, default=False):
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
            if fallback is not None:
                candidate["fallback"] = None if fallback == "none" else fallback
            config["profiles"][profile] = candidate
            if default:
                config["default_profile"] = profile
            self.save_config(config)
            return config

    def set_limits(self, parallel=None, attempts=None):
        with self.locked():
            config = copy.deepcopy(self.config())
            if parallel is not None:
                config["max_parallel_workers"] = parallel
            if attempts is not None:
                config["max_attempts_per_task"] = attempts
            self.save_config(config)
            return config

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
        return {"installed": True, "version": receipt["version"], "revision": receipt["revision"],
                "skill": str(self.skill), "launcher": str(self.launcher),
                "command_dir": str(self.command_dir), "menu_command": self.menu_command(),
                "local_code_changes": self.changed_files(receipt), "config": self.config(),
                "activation": activation.status(self.home, receipt.get("activation")),
                "main_session": "unchanged"}

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
            old_launchers = {path: path.read_bytes() if path.exists() else None for path in self.launchers()}
            backup = self.backup_path()
            with self.rules_transaction(rule_edits):
                self.skill.rename(backup)
                try:
                    for launcher in self.launchers():
                        if launcher.exists():
                            launcher.unlink()
                except BaseException:
                    backup.rename(self.skill)
                    self.restore_launchers(old_launchers)
                    raise
            return {"result": "uninstalled", "backup": str(backup)}


def run_git(arguments):
    try:
        result = subprocess.run(["git", *arguments], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagementError(f"Git 操作失败，请检查网络和 Git 是否可用：{exc}") from exc
    if result.returncode:
        raise ManagementError("Git 操作失败：" + (result.stderr.strip() or "请检查网络和仓库访问权限"))
    return result.stdout.strip()


def revision_of(root):
    if not (root / ".git").exists():
        return "local"
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode == 1:
        return "local"
    if result.returncode:
        raise ManagementError("无法读取源码提交编号：" + result.stderr.strip())
    return result.stdout.strip()


def profile_label(name):
    return {"default": "常规执行（default）", "complex": "困难任务（complex）"}.get(name, name)


def print_config(config):
    print(f"默认执行角色：{profile_label(config['default_profile'])}")
    print(f"最多并行子代理数：{config['max_parallel_workers']}")
    print(f"每个任务最多尝试次数：{config['max_attempts_per_task']}")
    for name, profile in config["profiles"].items():
        effort = profile["reasoning_effort"]
        print(f"\n  {profile_label(name)}")
        print(f"    模型：{profile['model']}")
        print(f"    思考强度：{EFFORT_LABELS.get(effort, effort)}（{effort}）")
        print(f"    后备角色：{profile_label(profile['fallback']) if profile.get('fallback') else '无'}")


def print_activation(state):
    print(f"默认委派：{'开启' if state['mode'] == 'auto' else '关闭（按需匹配）'}")
    if state.get("file"):
        print(f"启动规则：{state['file']}")
    if state.get("issue"):
        print(f"规则检查：{state['issue']}")
    elif state["mode"] == "auto":
        print("规则检查：已写入，新 Codex 会话加载后生效")


def print_result(value, *, human=False):
    if not human:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    if "installed" in value:
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


def prompt_count(stream, label, default):
    try:
        return int(prompt(stream, label, default))
    except ValueError as exc:
        raise ManagementError(f"{label}必须填写整数") from exc


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
            with open("/dev/tty", "r", encoding="utf-8") as terminal:
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
              "1. 安装 / 更新\n2. 设置执行模型和思考强度\n3. 设置并发数和尝试次数\n"
              "4. 查看状态和当前设置\n5. 回滚版本（保留执行设置）\n6. 卸载\n"
              "7. 开启 / 关闭默认委派\n0. 退出")
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
                name = prompt(stream, "执行角色名称（可输入已有名称或新名称）", config["default_profile"])
                current = config["profiles"].get(name, {})
                model = prompt(stream, "执行模型 ID", current.get("model"))
                effort = select_effort(stream, current.get("reasoning_effort", "medium"))
                fallback = prompt(stream, "后备角色名称（输入“无”关闭升级）", current.get("fallback") or "无")
                if fallback == "无":
                    fallback = "none"
                default = prompt(stream, "设为默认执行角色？是/否", "否").lower() in {"是", "y", "yes"}
                installation.configure(name, model, effort, fallback, default)
                print("执行配置已保存，主代理设置未改变。")
            elif choice == "3":
                config = installation.config()
                parallel = prompt_count(stream, "最多并行子代理数", config["max_parallel_workers"])
                attempts = prompt_count(stream, "每个任务最多尝试次数", config["max_attempts_per_task"])
                installation.set_limits(parallel, attempts)
                print("并发数和尝试次数已保存。")
            elif choice == "4":
                print_result(installation.status(), human=True)
            elif choice == "5":
                print_result(installation.rollback(), human=True)
                print("请新建 Codex 任务以重新加载技能。")
                return
            elif choice == "6":
                if prompt(stream, "输入“卸载”确认移除本技能，其他输入取消") in {"卸载", "uninstall"}:
                    print_result(installation.uninstall(), human=True)
                    return
            elif choice == "7":
                current = installation.status()
                if not current["installed"]:
                    raise ManagementError("请先选择菜单 1 安装本技能")
                print_activation(current["activation"])
                answer = prompt(stream, "默认委派：1 开启，2 关闭", "1" if current["activation"]["mode"] == "auto" else "2")
                if answer not in {"1", "2"}:
                    raise ManagementError("请输入 1 或 2")
                print_result(installation.set_mode("auto" if answer == "1" else "on-demand"), human=True)
            else:
                print("请输入 0 到 7 的菜单编号。")
        except EOFError:
            return
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"操作失败：{exc}", file=sys.stderr)


def main(argv=None):
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
    configure.add_argument("--fallback", help="已有的执行角色名称；none 表示无后备角色")
    configure.add_argument("--default", action="store_true")
    limits = commands.add_parser("limits")
    limits.add_argument("--parallel", type=int)
    limits.add_argument("--attempts", type=int)
    mode = commands.add_parser("mode", help="开启或关闭默认委派")
    mode.add_argument("value", choices=["auto", "on-demand"])
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        installation = Installation(args.codex_home, args.bin_dir)
        if args.command is None or args.command == "menu":
            menu(installation, args.source)
            return 0
        if args.command in {"install", "update"}:
            output = installation.install(args.source, require_existing=args.command == "update")
        elif args.command == "configure":
            output = installation.configure(args.profile, args.model, args.effort, args.fallback, args.default)
        elif args.command == "limits":
            output = installation.set_limits(args.parallel, args.attempts)
        elif args.command == "mode":
            output = installation.set_mode(args.value)
        elif args.command == "rollback":
            output = installation.rollback()
        elif args.command == "uninstall":
            if not args.yes:
                raise ManagementError("请使用 uninstall --yes，或在菜单中选择“卸载”")
            output = installation.uninstall()
        else:
            output = installation.status()
        print_result(output, human=sys.stdout.isatty())
        if args.command in {"install", "update", "rollback"}:
            installation.print_commands()
            print("请重新启动 Codex 会话以加载技能及默认委派规则。")
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
