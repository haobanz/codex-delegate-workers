#!/usr/bin/env python3
"""Project-scoped AGENTS.md rules with conservative, reversible file changes."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import platform_support
import workers


STATE_FILE = ".delegate-workers-project.json"
LOCK_FILE = ".delegate-workers-project.lock"
BACKUP_DIR = ".delegate-workers-project-backups"
INSTRUCTION_FILES = frozenset({"AGENTS.md", "AGENTS.override.md"})
BEGIN_TOKEN = b"<!-- delegate-workers:project:begin -->"
END_TOKEN = b"<!-- delegate-workers:project:end -->"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ProjectError(ValueError):
    """A project operation was refused without changing project files."""


def _read_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ProjectError(f"项目文件是符号链接，未进行修改：{path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ProjectError(f"项目文件不是普通文件，未进行修改：{path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectError(f"无法读取项目文件：{path}：{exc}") from exc


def _file_mode(path, default=0o644):
    path = Path(path)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ProjectError(f"项目文件不是安全的普通文件：{path}")
        return path.stat().st_mode & 0o777
    return default


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _atomic_write(path, data, mode):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".delegate-workers-project-", dir=str(path.parent))
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


def _validate_profile(value):
    if not isinstance(value, str) or not workers.PROFILE_NAME.fullmatch(value):
        raise ProjectError("项目状态中的 profile 无效")
    return value


def validate_state(value, *, validate_worker=False):
    """Validate the complete project state before a mutation is planned."""
    if not isinstance(value, dict):
        raise ProjectError("项目状态必须是 JSON 对象")
    expected = {"schema_version", "enabled", "file", "sha256", "created_file",
                "selection", "worker"}
    unknown = set(value) - expected
    missing = expected - set(value)
    if missing or unknown:
        detail = []
        if missing:
            detail.append("缺少字段：" + ", ".join(sorted(missing)))
        if unknown:
            detail.append("包含未知字段：" + ", ".join(sorted(unknown)))
        raise ProjectError("项目状态格式无效（" + "；".join(detail) + "）")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ProjectError("项目状态 schema_version 必须为 1")
    if type(value["enabled"]) is not bool:
        raise ProjectError("项目状态 enabled 必须是布尔值")
    if not isinstance(value["file"], str) or value["file"] not in INSTRUCTION_FILES:
        raise ProjectError("项目状态中的指令文件名不在允许范围内")
    if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
        raise ProjectError("项目状态中的指令块校验值无效")
    if type(value["created_file"]) is not bool:
        raise ProjectError("项目状态 created_file 必须是布尔值")
    selection = value["selection"]
    if not isinstance(selection, dict) or set(selection) != {"profile"}:
        raise ProjectError("项目状态 selection 格式无效")
    _validate_profile(selection["profile"])
    worker = value["worker"]
    if (not isinstance(worker, dict) or set(worker) != {"model", "reasoning_effort"}
            or not isinstance(worker["model"], str)
            or not isinstance(worker["reasoning_effort"], str)):
        raise ProjectError("项目状态 worker 格式无效")
    try:
        workers.check_model(worker["model"])
        workers.check_effort(worker["reasoning_effort"])
    except Exception as exc:
        raise ProjectError(f"项目状态 worker 格式无效：{exc}") from exc
    if validate_worker:
        try:
            workers.validate_worker(worker)
        except Exception as exc:
            raise ProjectError(f"项目状态中的模型选择无效：{exc}") from exc
    return value


def _read_state(root):
    path = Path(root) / STATE_FILE
    raw = _read_file(path)
    if raw is None:
        return None, None
    try:
        value = workers.read_json(path)
    except Exception as exc:
        raise ProjectError(f"项目状态 JSON 无效：{path}：{exc}") from exc
    validate_state(value)
    return value, raw


def _clean_git_environment():
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("GIT_"):
            environment.pop(name, None)
    # Keep repository/error classification stable across localized hosts.
    environment["LC_ALL"] = "C"
    return environment


def _requested_directory(path):
    explicit = path is not None
    requested = Path.cwd() if path is None else Path(path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        requested = requested.resolve(strict=True)
    except OSError as exc:
        raise ProjectError(f"项目路径不存在或无法解析：{path or Path.cwd()}：{exc}") from exc
    if not requested.is_dir():
        raise ProjectError(f"项目路径不是目录：{requested}")
    return requested, explicit


def _reject_unsafe_root(root):
    root = Path(root)
    if root.parent == root:
        raise ProjectError("不能把文件系统根目录作为项目根目录")
    try:
        if root == Path.home().resolve(strict=True):
            raise ProjectError("不能把用户主目录作为项目根目录")
    except OSError:
        pass


def _is_non_repository_error(result):
    text = (result.stderr or "").lower()
    return ("not a git repository" in text
            or "not a git work tree" in text
            or "outside a repository" in text)


def resolve_location(path=None, *, mutation=False):
    """Resolve a requested directory to its actual worktree root when possible."""
    requested, explicit = _requested_directory(path)
    try:
        result = subprocess.run(
            ["git", "-C", str(requested), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, shell=False, env=_clean_git_environment(), check=False)
    except FileNotFoundError as exc:
        if explicit and not mutation:
            # Read-only inspection can safely inspect the explicit directory;
            # it must not pretend to have found a nested Git worktree.
            root = requested
            git = False
        elif explicit and mutation:
            raise ProjectError("无法使用 Git 解析项目根目录；项目修改需要可用的 git") from exc
        else:
            raise ProjectError("无法使用 Git 解析项目根目录；未找到 git") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectError("Git 解析项目根目录超时，未进行修改") from exc
    except OSError as exc:
        raise ProjectError(f"Git 解析项目根目录失败：{exc}") from exc
    else:
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            if len(lines) != 1 or not lines[0].strip():
                raise ProjectError("Git 返回了无法验证的项目根目录，未进行修改")
            candidate = Path(lines[0]).expanduser()
            if not candidate.is_absolute():
                raise ProjectError("Git 返回了相对项目根目录，未进行修改")
            try:
                root = candidate.resolve(strict=True)
            except OSError as exc:
                raise ProjectError(f"Git 返回的项目根目录无法解析：{candidate}") from exc
            if not root.is_dir():
                raise ProjectError("Git 返回的项目根目录不是目录，未进行修改")
            try:
                requested.relative_to(root)
            except ValueError as exc:
                raise ProjectError("Git 返回的根目录不包含请求路径，已拒绝不相关仓库") from exc
            git = True
        elif _is_non_repository_error(result) and explicit:
            root = requested
            git = False
        else:
            detail = (result.stderr or result.stdout or "未知 Git 错误").strip()
            raise ProjectError(f"无法安全解析 Git 项目根目录：{detail}")
    _reject_unsafe_root(root)
    return {"requested": requested, "root": root, "explicit": explicit, "git": git}


def resolve_project_root(path=None, *, mutation=False):
    """Public convenience wrapper returning only the resolved root."""
    return resolve_location(path, mutation=mutation)["root"]


@contextlib.contextmanager
def project_lock(root):
    """Lock one project independently of the caller's CODEX_HOME."""
    path = Path(root) / LOCK_FILE
    if path.is_symlink():
        raise ProjectError(f"项目锁是符号链接，未进行修改：{path}")
    if path.exists() and not path.is_file():
        raise ProjectError(f"项目锁不是普通文件，未进行修改：{path}")
    try:
        with platform_support.file_lock(path):
            yield
    except BlockingIOError as exc:
        raise ProjectError("另一个 Codex home 正在修改此项目，请稍后重试") from exc


def _instruction_paths(root):
    return {name: Path(root) / name for name in sorted(INSTRUCTION_FILES)}


def _read_instructions(root):
    paths = _instruction_paths(root)
    return {name: _read_file(path) for name, path in paths.items()}


def _effective_name(contents):
    override = contents["AGENTS.override.md"]
    return "AGENTS.override.md" if override is not None and override.strip() else "AGENTS.md"


def _line_ending(data):
    return b"\r\n" if b"\r\n" in (data or b"") else b"\n"


def _render_block(template, worker, newline):
    try:
        text = template.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectError("项目委派模板不是有效的 UTF-8") from exc
    if "{model}" not in text or "{reasoning_effort}" not in text:
        raise ProjectError("项目委派模板缺少 model 或 reasoning_effort 占位符")
    text = text.replace("{model}", worker["model"])
    text = text.replace("{reasoning_effort}", worker["reasoning_effort"])
    if BEGIN_TOKEN.decode() in text or END_TOKEN.decode() in text:
        raise ProjectError("项目委派模板包含保留标记")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    body = normalized.replace("\n", newline.decode()).encode("utf-8")
    # The leading newline belongs to the owned block. This makes removal
    # byte-exact for every original ending, including an empty file.
    return newline + BEGIN_TOKEN + newline + body + newline + END_TOKEN + newline


def _marker_info(data):
    if data is None:
        return None
    begins = data.count(BEGIN_TOKEN)
    ends = data.count(END_TOKEN)
    if not begins and not ends:
        return None
    if begins != 1 or ends != 1:
        raise ProjectError("项目指令文件中的委派标记缺失、重复或不完整")
    marker_start = data.index(BEGIN_TOKEN)
    newline_start = marker_start + len(BEGIN_TOKEN)
    if data[newline_start:newline_start + 2] == b"\r\n":
        newline = b"\r\n"
    elif data[newline_start:newline_start + 1] == b"\n":
        newline = b"\n"
    else:
        raise ProjectError("项目委派开始标记缺少所属的换行，未进行修改")
    if data[marker_start - len(newline):marker_start] != newline:
        raise ProjectError("项目委派开始标记缺少所属的换行，未进行修改")
    start = marker_start - len(newline)
    end_marker = data.index(END_TOKEN)
    if end_marker < start:
        raise ProjectError("项目指令文件中的委派标记顺序无效")
    end = end_marker + len(END_TOKEN)
    if data[end:end + 2] == b"\r\n":
        end += 2
    elif data[end:end + 1] == b"\n":
        end += 1
    elif end < len(data):
        raise ProjectError("项目委派结束标记没有独立成行")
    return {"start": start, "end": end, "block": data[start:end],
            "sha256": hashlib.sha256(data[start:end]).hexdigest()}


def _remove_block(data, info):
    return data[:info["start"]] + data[info["end"]:]


def _append_block(data, block, newline):
    data = b"" if data is None else data
    return data + block


def _validate_markers_for_mutation(state, contents):
    infos = {}
    for name, data in contents.items():
        info = _marker_info(data)
        if info is not None:
            infos[name] = info
            if state is None or not state["enabled"] or name != state["file"]:
                raise ProjectError(f"发现未登记的项目委派标记，未进行修改：{name}")
            if info["sha256"] != state["sha256"]:
                raise ProjectError(f"项目委派块已被修改，未覆盖用户修改：{name}")
    if state is not None and state["enabled"]:
        if state["file"] not in infos:
            raise ProjectError("项目状态记录的委派块缺失，未进行修改")
    return infos


def _nested_guidance(root, requested, *, strict):
    try:
        relative = Path(requested).relative_to(root)
    except ValueError as exc:
        raise ProjectError("请求路径不在已解析项目根目录内") from exc
    result = []
    parts = relative.parts
    for index in range(1, len(parts) + 1):
        directory = Path(root).joinpath(*parts[:index])
        for name in sorted(INSTRUCTION_FILES):
            path = directory / name
            if path.is_symlink():
                if strict:
                    raise ProjectError(f"嵌套指令文件是符号链接，未进行修改：{path}")
                result.append({"path": str(path), "kind": "symlink"})
            elif path.exists():
                if not path.is_file():
                    if strict:
                        raise ProjectError(f"嵌套指令路径不是普通文件：{path}")
                    result.append({"path": str(path), "kind": "not-a-file"})
                else:
                    result.append({"path": str(path), "kind": "potential-override"})
    return result


def _safe_backup_root(root, *, create=False):
    path = Path(root) / BACKUP_DIR
    if path.is_symlink():
        raise ProjectError(f"项目备份目录是符号链接，未进行修改：{path}")
    if path.exists() and not path.is_dir():
        raise ProjectError(f"项目备份路径不是目录，未进行修改：{path}")
    if create and not path.exists():
        path.mkdir(mode=0o700)
    return path


def _backup(root, edits, operation):
    parent = _safe_backup_root(root, create=True)
    try:
        directory = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                              + "-" + uuid4().hex[:8])
        directory.mkdir(mode=0o700)
        files = []
        for edit in edits:
            relative = edit["path"].name
            if edit["before"] is not None:
                destination = directory / relative
                _atomic_write(destination, edit["before"], edit["mode"])
                files.append({"name": relative, "sha256": hashlib.sha256(edit["before"]).hexdigest(),
                              "mode": edit["mode"]})
            else:
                files.append({"name": relative, "absent": True})
        manifest = {"schema_version": 1, "operation": operation,
                    "created_at": datetime.now(timezone.utc).isoformat(), "files": files}
        _atomic_write(directory / "manifest.json", _json_bytes(manifest), 0o600)
        return directory
    except BaseException:
        if 'directory' in locals() and directory.exists():
            shutil.rmtree(directory)
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        raise


def _rollback(edits):
    errors = []
    for edit in reversed(edits):
        path = edit["path"]
        try:
            current = _read_file(path)
            if current != edit["after"]:
                errors.append(f"回滚时发现外部修改：{path}")
                continue
            if edit["before"] is None:
                if path.is_symlink():
                    errors.append(f"回滚时发现符号链接：{path}")
                elif path.exists():
                    path.unlink()
            else:
                _atomic_write(path, edit["before"], edit["mode"])
        except (OSError, ProjectError) as exc:
            errors.append(str(exc))
    return errors


def _apply(root, edits, operation):
    edits = [edit for edit in edits if edit["before"] != edit["after"]]
    if not edits:
        return None
    backup = _backup(root, edits, operation)
    applied = []
    try:
        for edit in edits:
            current = _read_file(edit["path"])
            if current != edit["before"]:
                raise ProjectError(f"项目文件在操作期间发生变化，请重试：{edit['path']}")
            if edit["after"] is None:
                if edit["path"].is_symlink():
                    raise ProjectError(f"项目文件变成了符号链接，请重试：{edit['path']}")
                if edit["path"].exists():
                    edit["path"].unlink()
            else:
                _atomic_write(edit["path"], edit["after"], edit["mode"])
            applied.append(edit)
    except BaseException as exc:
        rollback_errors = _rollback(applied)
        if rollback_errors:
            raise ProjectError(str(exc) + "；自动回滚未完成，请使用备份恢复：" + str(backup)
                               + "；" + "；".join(rollback_errors)) from exc
        shutil.rmtree(backup)
        parent = backup.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        raise
    return backup


def _state_value(file_name, block, created_file, profile, worker, enabled=True):
    return {"schema_version": 1, "enabled": enabled, "file": file_name,
            "sha256": hashlib.sha256(block).hexdigest(), "created_file": created_file,
            "selection": {"profile": profile},
            "worker": {"model": worker["model"], "reasoning_effort": worker["reasoning_effort"]}}


def _state_edit(root, before, value):
    path = Path(root) / STATE_FILE
    after = _json_bytes(value)
    if before is not None:
        try:
            if json.loads(before.decode("utf-8")) == value:
                after = before
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {"path": path, "before": before, "after": after,
            "mode": _file_mode(path, 0o600)}


def _instruction_edit(root, name, before, after):
    path = Path(root) / name
    return {"path": path, "before": before, "after": after, "mode": _file_mode(path)}


def _load_template(template_path):
    path = Path(template_path)
    if path.is_symlink() or not path.is_file():
        raise ProjectError(f"项目委派模板不存在或不安全：{path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectError(f"无法读取项目委派模板：{path}：{exc}") from exc


def _validated_worker(worker):
    if not isinstance(worker, dict):
        raise ProjectError("项目执行模型选择无效")
    candidate = {"model": worker.get("model"), "reasoning_effort": worker.get("reasoning_effort")}
    try:
        compatibility = workers.validate_worker(candidate)
    except Exception as exc:
        raise ProjectError(f"项目执行模型选择无效：{exc}") from exc
    return candidate, compatibility


def _validate_worker(worker):
    return _validated_worker(worker)[0]


def _diagnose_worker(worker):
    try:
        _, compatibility = _validated_worker(worker)
        return compatibility
    except Exception as exc:
        return {"status": "incompatible", "source": "静态兼容性预检",
                "runtime_verified": False, "error": str(exc)}


def _selection_from_state(state):
    return state["selection"]["profile"], dict(state["worker"])


def init_project(path, *, template_path, profile=None, worker=None, default_selection=None,
                 selection_resolver=None):
    """Enable or reconfigure project rules; default_selection is called only for a new project."""
    explicit = selection_resolver is not None or profile is not None or worker is not None
    if explicit and selection_resolver is None:
        if profile is None or worker is None:
            raise ProjectError("项目选择必须同时包含 profile 和 worker")
        profile = _validate_profile(profile)
        worker, compatibility = _validated_worker(worker)
    else:
        compatibility = None
    location = resolve_location(path, mutation=True)
    root = location["root"]
    _nested_guidance(root, location["requested"], strict=True)
    with project_lock(root):
        state, state_bytes = _read_state(root)
        if state is not None:
            validate_state(state)
        if selection_resolver is not None:
            profile, worker = selection_resolver(state)
            profile = _validate_profile(profile)
            worker, compatibility = _validated_worker(worker)
        elif not explicit:
            if state is not None:
                profile, worker = _selection_from_state(state)
                worker, compatibility = _validated_worker(worker)
            elif default_selection is not None:
                profile, worker = default_selection()
                profile = _validate_profile(profile)
                worker, compatibility = _validated_worker(worker)
            else:
                raise ProjectError("尚未提供项目执行模型选择")
        contents = _read_instructions(root)
        infos = _validate_markers_for_mutation(state, contents)
        template = _load_template(template_path)
        old_name = state["file"] if state is not None and state["enabled"] else None
        old_info = infos.get(old_name) if old_name else None
        target_name = _effective_name(contents)
        target_before = contents[target_name]
        newline = _line_ending(target_before)
        block = _render_block(template, worker, newline)
        edits = []
        if old_name is not None:
            old_before = contents[old_name]
            old_after = _remove_block(old_before, old_info)
            if old_name != target_name:
                if state["created_file"] and old_after == b"":
                    old_after = None
                edits.append(_instruction_edit(root, old_name, old_before, old_after))
                target_after = _append_block(target_before, block, newline)
            else:
                target_after = old_before[:old_info["start"]] + block + old_before[old_info["end"]:]
        else:
            target_after = _append_block(target_before, block, newline)
        created_file = (state["created_file"] if state is not None and old_name == target_name
                        else target_before is None)
        edits.append(_instruction_edit(root, target_name, target_before, target_after))
        new_state = _state_value(target_name, block, created_file, profile, worker, enabled=True)
        edits.append(_state_edit(root, state_bytes, new_state))
        backup = _apply(root, edits, "init")
    return {"project": "init", "result": "initialized" if state is None else
            ("reenabled" if not state["enabled"] else ("updated" if backup else "unchanged")),
            "project_root": str(root), "requested_path": str(location["requested"]),
            "git_worktree": location["git"], "enabled": True, "file": target_name,
            "selection": {"profile": profile}, "worker": worker,
            "compatibility": compatibility,
            "state_file": str(root / STATE_FILE), "backup": str(backup) if backup else None,
            "recovery": "备份已保留；如需人工恢复，请检查 backup 路径" if backup else None}


def sync_project(path, *, template_path):
    location = resolve_location(path, mutation=True)
    root = location["root"]
    _nested_guidance(root, location["requested"], strict=True)
    with project_lock(root):
        state, state_bytes = _read_state(root)
        if state is None:
            raise ProjectError("项目尚未登记，sync 不会自动启用；请先运行 project init")
        validate_state(state)
        if not state["enabled"]:
            raise ProjectError("项目已禁用，sync 不会重新启用；请先运行 project init")
        worker, compatibility = _validated_worker(state["worker"])
        contents = _read_instructions(root)
        infos = _validate_markers_for_mutation(state, contents)
        template = _load_template(template_path)
        old_name = state["file"]
        old_info = infos[old_name]
        target_name = _effective_name(contents)
        target_before = contents[target_name]
        newline = _line_ending(target_before)
        block = _render_block(template, worker, newline)
        edits = []
        old_before = contents[old_name]
        old_after = _remove_block(old_before, old_info)
        if old_name != target_name:
            if state["created_file"] and old_after == b"":
                old_after = None
            edits.append(_instruction_edit(root, old_name, old_before, old_after))
            target_after = _append_block(target_before, block, newline)
        else:
            target_after = old_before[:old_info["start"]] + block + old_before[old_info["end"]:]
        created_file = target_before is None if target_name != old_name else state["created_file"]
        new_state = _state_value(target_name, block, created_file, state["selection"]["profile"],
                                 worker, enabled=True)
        edits.append(_instruction_edit(root, target_name, target_before, target_after))
        edits.append(_state_edit(root, state_bytes, new_state))
        backup = _apply(root, edits, "sync")
    return {"project": "sync", "result": "synced" if backup else "unchanged",
            "project_root": str(root), "requested_path": str(location["requested"]),
            "git_worktree": location["git"], "enabled": True, "file": target_name,
            "selection": state["selection"], "worker": worker, "compatibility": compatibility,
            "state_file": str(root / STATE_FILE), "backup": str(backup) if backup else None,
            "recovery": "备份已保留；如需人工恢复，请检查 backup 路径" if backup else None}


def disable_project(path):
    location = resolve_location(path, mutation=True)
    root = location["root"]
    _nested_guidance(root, location["requested"], strict=True)
    with project_lock(root):
        state, state_bytes = _read_state(root)
        if state is None:
            raise ProjectError("项目尚未登记，disable 未进行修改")
        validate_state(state)
        if not state["enabled"]:
            compatibility = _diagnose_worker(state["worker"])
            return {"project": "disable", "result": "unchanged", "project_root": str(root),
                    "requested_path": str(location["requested"]), "git_worktree": location["git"],
                    "enabled": False, "worker": state["worker"], "compatibility": compatibility,
                    "state_file": str(root / STATE_FILE), "backup": None,
                    "recovery": None}
        contents = _read_instructions(root)
        infos = _validate_markers_for_mutation(state, contents)
        name = state["file"]
        before = contents[name]
        remainder = _remove_block(before, infos[name])
        after = None if state["created_file"] and remainder == b"" else remainder
        edits = [_instruction_edit(root, name, before, after)]
        disabled = dict(state)
        disabled["enabled"] = False
        edits.append(_state_edit(root, state_bytes, disabled))
        backup = _apply(root, edits, "disable")
    return {"project": "disable", "result": "disabled", "project_root": str(root),
            "requested_path": str(location["requested"]), "git_worktree": location["git"],
            "enabled": False, "file": name, "selection": state["selection"],
            "worker": state["worker"], "compatibility": _diagnose_worker(state["worker"]),
            "state_file": str(root / STATE_FILE),
            "backup": str(backup) if backup else None,
            "recovery": "备份已保留；如需人工恢复，请检查 backup 路径" if backup else None}


def _status_integrity(state, contents):
    if state is None:
        for data in contents.values():
            if data is not None and (BEGIN_TOKEN in data or END_TOKEN in data):
                return "orphaned", "发现未登记的项目委派标记，请人工检查后再运行 init"
        return "not_registered", None
    if not state["enabled"]:
        for data in contents.values():
            if data is not None and (BEGIN_TOKEN in data or END_TOKEN in data):
                return "orphaned", "项目已禁用但仍有项目委派标记，请人工检查"
        return "disabled", None
    data = contents.get(state["file"])
    if data is None:
        return "missing", "项目状态记录的指令文件不存在；请恢复文件或人工检查状态"
    try:
        info = _marker_info(data)
    except ProjectError as exc:
        return "changed", str(exc)
    if info is None or info["sha256"] != state["sha256"]:
        return "changed", "项目委派块已被修改；不会自动覆盖用户编辑，请人工恢复或移除标记"
    for name, other in contents.items():
        if name != state["file"] and other is not None and (BEGIN_TOKEN in other or END_TOKEN in other):
            return "orphaned", f"{name} 中存在额外项目委派标记，请人工检查"
    return "ok", None


def status_project(path):
    location = resolve_location(path, mutation=False)
    root = location["root"]
    contents = _read_instructions(root)
    state_path = root / STATE_FILE
    state_raw = None
    state = None
    state_issue = None
    try:
        state_raw = _read_file(state_path)
        if state_raw is not None:
            try:
                candidate = workers.read_json(state_path)
                validate_state(candidate)
                state = candidate
            except Exception as exc:
                state_issue = str(exc)
    except ProjectError as exc:
        state_issue = str(exc)
    nested = _nested_guidance(root, location["requested"], strict=False)
    effective = _effective_name(contents)
    integrity, integrity_issue = ("invalid_state", state_issue) if state_issue else _status_integrity(state, contents)
    file_name = state["file"] if state is not None and state.get("file") in INSTRUCTION_FILES else effective
    file_present = contents[file_name] is not None
    scope_warnings = []
    if state is not None and state.get("enabled") and state.get("file") != effective:
        scope_warnings.append(
            f"根目录非空 {effective} 当前优先于状态记录的 {state['file']}；显式 init 或 sync 可迁移完整委派块")
    if nested:
        scope_warnings.append("请求工作目录下存在潜在的嵌套指令覆盖；仅列出路径，未判断语义冲突")
    if file_present and len(contents[file_name]) > 32768:
        scope_warnings.append("有效指令文件超过 Codex 默认约 32 KiB 上下文限制，是否完整加载未知")
    diagnostics = []
    if state_issue:
        diagnostics.append("项目状态无效：" + state_issue)
    if integrity_issue:
        diagnostics.append(integrity_issue)
    compatibility = _diagnose_worker(state["worker"]) if state is not None else None
    if state is not None and compatibility.get("status") not in {"compatible", "unverified"}:
        diagnostics.append("项目执行模型静态预检失败：" + compatibility.get("error", "未知错误"))
    elif state is not None and compatibility.get("warning"):
        diagnostics.append("项目执行模型尚未得到能力快照验证：" + compatibility["warning"])
    if state_raw is None:
        diagnostics.append("项目尚未登记；运行 dw project init --path PATH 以选择并启用执行模型")
    elif state is not None and not state["enabled"]:
        diagnostics.append("项目已禁用；sync 不会自动启用，运行 dw project init 重新启用")
    if scope_warnings:
        diagnostics.extend(scope_warnings)
    return {
        "project": "status", "project_root": str(root),
        "requested_path": str(location["requested"]), "git_worktree": location["git"],
        "state_file": str(state_path), "state_present": state_raw is not None,
        "state_valid": state_issue is None and state_raw is not None,
        "enabled": state.get("enabled") if state is not None else False,
        "status": integrity if state_issue is None else "invalid_state",
        "file": file_name, "effective_file": effective,
        "file_present": file_present, "integrity": integrity,
        "scope_warnings": scope_warnings, "nested_guidance": nested,
        "selection": state.get("selection") if state is not None else None,
        "worker": state.get("worker") if state is not None else None,
        "session_loaded": None, "runtime_verified": False,
        "compatibility": compatibility,
        "diagnostics": diagnostics,
        "action": ("请重新启动或重新读取 Codex 任务以加载项目指令；当前工具不能确认活动会话已加载。"
                   if integrity == "ok" else "请先按 diagnostics 检查项目状态；工具不会自动覆盖人工编辑。"),
        "backup_dir": str(Path(root) / BACKUP_DIR),
    }
