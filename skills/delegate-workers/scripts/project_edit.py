#!/usr/bin/env python3
"""Prepare, review and transactionally adopt complete project instruction drafts."""

import difflib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import project_rules as rules


MAX_DOCUMENT_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
BASELINE_FILES = tuple(sorted(rules.INSTRUCTION_FILES)) + (rules.STATE_FILE,)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bounded_read(path, limit=MAX_DOCUMENT_BYTES):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise rules.ProjectError(f"草稿或项目文件不是安全的普通文件：{path}")
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise rules.ProjectError(f"无法读取文件：{path}：{exc}") from exc
    if len(raw) > limit:
        raise rules.ProjectError(f"文件超过编辑长度限制（{limit} 字节）：{path}")
    return raw


def _utf8(raw, label):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise rules.ProjectError(f"{label} 不是有效的 UTF-8 文本") from exc


def _snapshot(root):
    result = {}
    contents = {}
    for name in BASELINE_FILES:
        raw = _bounded_read(root / name, MAX_MANIFEST_BYTES if name == rules.STATE_FILE
                            else MAX_DOCUMENT_BYTES)
        contents[name] = raw
        result[name] = {"sha256": _sha(raw) if raw is not None else None,
                        "mode": rules._file_mode(root / name) if raw is not None else None}
    return result, contents


def _candidate_details(raw, worker, newline):
    if raw is None or not raw.strip() or len(raw) > MAX_DOCUMENT_BYTES:
        raise rules.ProjectError("候选规则必须是长度不超过 128 KiB 的非空完整文档")
    text = _utf8(raw, "候选规则")
    if text.lstrip("\ufeff \t\r\n").startswith(("```", "~~~")):
        raise rules.ProjectError("候选规则不能用代码围栏包装；请提供完整 Markdown 文档")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if (lines.count(rules.BEGIN_TOKEN.decode()) != 1
            or lines.count(rules.END_TOKEN.decode()) != 1):
        raise rules.ProjectError("候选规则必须保留唯一且独立成行的项目委派标记")
    data = normalized.replace("\n", newline.decode()).encode("utf-8")
    info = rules._marker_info(data)
    if info is None:
        raise rules.ProjectError("候选规则缺少项目委派块")
    body = normalized.split(rules.BEGIN_TOKEN.decode() + "\n", 1)[1]
    body = body.split(rules.END_TOKEN.decode(), 1)[0].rstrip("\n") + "\n"
    for name, expected in (("model", worker["model"]),
                           ("reasoning effort", worker["reasoning_effort"])):
        matching = [line for line in body.splitlines() if line.startswith(f"- {name}:")]
        if matching != [f"- {name}: `{expected}`"]:
            raise rules.ProjectError(f"候选规则的 {name} 行必须与项目选择一致且唯一")
    # Protect declarations before converting repeated route references: a
    # valid short ID such as "model" or "max" must not replace field names or
    # the separate effort declaration. Ambiguous body references retain the
    # model-first replacement order; the script does not infer their meaning.
    declarations = {
        f"- model: `{worker['model']}`": "- model: `{model}`",
        f"- reasoning effort: `{worker['reasoning_effort']}`":
        "- reasoning effort: `{reasoning_effort}`",
    }
    template_lines = []
    for line in body.split("\n"):
        if line in declarations:
            template_lines.append(declarations[line])
        else:
            rebound = line.replace(worker["model"], "{model}")
            template_lines.append(rebound.replace("`" + worker["reasoning_effort"] + "`",
                                                 "`{reasoning_effort}`"))
    template = "\n".join(template_lines)
    rules._validate_custom_rule(template)
    # Preview the exact block that future init/sync will render, including its
    # owned newlines, so adoption cannot be changed by the first later sync.
    rendered = rules._render_block(template.encode("utf-8"), worker, newline)
    data = data[:info["start"]] + rendered + data[info["end"]:]
    info = rules._marker_info(data)
    return data, info, template


def _validate_active(state, contents):
    if state is None:
        raise rules.ProjectError("项目尚未登记；请先运行 dw project init")
    if not state["enabled"]:
        raise rules.ProjectError("项目已禁用；请先运行 dw project init 重新启用")
    instruction_contents = {name: contents[name] for name in rules.INSTRUCTION_FILES}
    if rules._effective_name(instruction_contents) != state["file"]:
        raise rules.ProjectError("活动指令文件与项目记录不同；请先运行 dw project sync")
    for name in rules.INSTRUCTION_FILES:
        if name != state["file"] and rules._marker_info(contents[name]) is not None:
            raise rules.ProjectError(f"另一个指令文件含有额外委派块：{name}")
    # An explicit editing session can review and readopt a manually edited
    # block. Other project mutations continue to require its original SHA.
    _candidate_details(contents[state["file"]], state["worker"],
                       rules._line_ending(contents[state["file"]]))


def _prepared_value(draft, manifest):
    return {"project": "edit", "result": "prepared", "project_root": manifest["project_root"],
            "file": manifest["file"], "worker": dict(manifest["state"]["worker"]),
            "draft_dir": str(draft), "candidate": str(draft / "candidate.md"),
            "prompt": str(draft / "prompt.txt"), "manifest": str(draft / "manifest.json")}


def _prompt(original, request, worker, file_name):
    return (
        "任务：根据明确的修改要求，编辑项目指令全文。\n"
        "你是执行代理。主代理或用户随后负责审查差异和最终验收。\n"
        "只完成本次要求，保留未要求修改的项目规则；不要扩大范围或继续派发代理。\n"
        "不修改任何项目文件，也不要执行项目操作。只在最终答复中输出完整 Markdown 文档，"
        "不要代码围栏、摘要或解释，不用输出补丁。\n"
        "发现要求不合理时可保留原规则并将具体异议交给主代理，不自行发明解决方案。\n"
        f"编辑目标：{file_name} 全文。必须保留唯一的项目委派 begin/end 标记。\n"
        f"必须保留且只出现一次：- model: `{worker['model']}`\n"
        f"必须保留且只出现一次：- reasoning effort: `{worker['reasoning_effort']}`\n"
        "不得在本次规则编辑中改变模型、思考强度或供应商。\n\n"
        "修改要求：\n" + (request or "由主代理在交接时给出具体要求；暂保留全部现有规则。")
        + "\n\n当前完整文档：\n" + _utf8(original, "项目指令")
    )


def prepare_edit(path=None, *, request=""):
    if not isinstance(request, str) or len(request.encode("utf-8")) > 32768:
        raise rules.ProjectError("修改要求必须是长度不超过 32 KiB 的文本")
    location = rules.resolve_location(path, mutation=True)
    root = location["root"]
    rules._nested_guidance(root, location["requested"], strict=True)
    with rules.project_lock(root):
        baseline, contents = _snapshot(root)
        state, state_bytes = rules._read_state(root)
        if state_bytes != contents[rules.STATE_FILE]:
            raise rules.ProjectError("项目状态在准备期间发生变化，请重试")
        _validate_active(state, contents)
        draft = Path(tempfile.mkdtemp(prefix="delegate-workers-project-edit-",
                                      dir=str(rules._project_tmp(root))))
        manifest = {"schema_version": 1, "project_root": str(root), "file": state["file"],
                    "state": state, "baseline": baseline,
                    "newline": "crlf" if rules._line_ending(contents[state["file"]]) == b"\r\n"
                    else "lf"}
        try:
            for name, data in (("candidate.md", contents[state["file"]]),
                               ("prompt.txt", _prompt(contents[state["file"]], request,
                                                     state["worker"], state["file"]).encode("utf-8")),
                               ("manifest.json", rules._json_bytes(manifest))):
                (draft / name).write_bytes(data)
                os.chmod(draft / name, 0o600)
        except BaseException:
            # This directory is new and wholly owned by this prepare call.
            import shutil
            shutil.rmtree(draft)
            raise
    return _prepared_value(draft, manifest)


def _load_draft(draft_dir):
    draft = Path(draft_dir).expanduser()
    if not draft.is_absolute():
        draft = Path.cwd() / draft
    if draft.is_symlink() or not draft.is_dir():
        raise rules.ProjectError("草稿路径必须是已经存在的普通目录")
    # Reject symlinked ancestors too: resolving just the leaf could silently
    # accept a redirect outside the project.
    if draft.resolve() != draft:
        raise rules.ProjectError("草稿目录不得通过符号链接或相对路径重定向")
    raw = _bounded_read(draft / "manifest.json", MAX_MANIFEST_BYTES)
    if raw is None:
        raise rules.ProjectError("草稿 manifest.json 不存在")
    try:
        manifest = json.loads(_utf8(raw, "草稿清单"))
    except json.JSONDecodeError as exc:
        raise rules.ProjectError("草稿清单不是有效的 JSON") from exc
    expected = {"schema_version", "project_root", "file", "state", "baseline", "newline"}
    if (not isinstance(manifest, dict) or set(manifest) != expected
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest["project_root"], str)
            or not isinstance(manifest["newline"], str)
            or manifest["newline"] not in {"lf", "crlf"}):
        raise rules.ProjectError("草稿清单格式无效")
    root = Path(manifest["project_root"])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise rules.ProjectError("草稿项目根目录无效或发生重定向")
    rules._reject_unsafe_root(root)
    if rules.resolve_project_root(root, mutation=True) != root:
        raise rules.ProjectError("草稿绑定的项目根目录已经变化")
    temporary_root = root / "tmp"
    if temporary_root.is_symlink() or not temporary_root.is_dir():
        raise rules.ProjectError("草稿项目临时目录发生变化")
    try:
        draft.relative_to(temporary_root)
    except ValueError as exc:
        raise rules.ProjectError("草稿必须位于绑定项目的 tmp 目录内") from exc
    if draft == temporary_root:
        raise rules.ProjectError("项目 tmp 根目录不能作为草稿")
    rules.validate_state(manifest["state"])
    if not manifest["state"]["enabled"] or manifest["file"] != manifest["state"]["file"]:
        raise rules.ProjectError("草稿活动文件或项目状态无效")
    baseline = manifest["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != set(BASELINE_FILES):
        raise rules.ProjectError("草稿基线文件清单无效")
    for item in baseline.values():
        if not isinstance(item, dict) or set(item) != {"sha256", "mode"}:
            raise rules.ProjectError("草稿文件基线格式无效")
        digest, mode = item["sha256"], item["mode"]
        if digest is None:
            if mode is not None:
                raise rules.ProjectError("不存在的基线文件不能有 mode")
        elif (not isinstance(digest, str) or not rules.SHA256.fullmatch(digest)
              or type(mode) is not int or not 0 <= mode <= 0o777):
            raise rules.ProjectError("草稿文件基线摘要或 mode 无效")
    if baseline[manifest["file"]]["sha256"] is None or baseline[rules.STATE_FILE]["sha256"] is None:
        raise rules.ProjectError("草稿缺少活动规则或状态基线")
    return draft, root, manifest


def _review_data(draft, root, manifest):
    raw = _bounded_read(draft / "candidate.md")
    candidate_sha = _sha(raw) if raw is not None else None
    newline = b"\r\n" if manifest["newline"] == "crlf" else b"\n"
    current, contents = _snapshot(root)
    if newline != rules._line_ending(contents[manifest["file"]]):
        raise rules.ProjectError("草稿行尾设置与绑定项目规则的行尾不一致；未应用")
    candidate, info, template = _candidate_details(raw, manifest["state"]["worker"], newline)
    expected_state = dict(manifest["state"])
    expected_state.update(sha256=info["sha256"], custom_rule=template)
    already_applied = False
    if current != manifest["baseline"]:
        others_unchanged = all(current[name] == manifest["baseline"][name]
                               for name in rules.INSTRUCTION_FILES if name != manifest["file"])
        try:
            existing_state = json.loads(_utf8(contents[rules.STATE_FILE], "项目状态"))
        except (TypeError, AttributeError, json.JSONDecodeError, rules.ProjectError):
            existing_state = None
        already_applied = (others_unchanged and contents[manifest["file"]] == candidate
                           and existing_state == expected_state
                           and current[manifest["file"]]["mode"] == manifest["baseline"][manifest["file"]]["mode"]
                           and current[rules.STATE_FILE]["mode"] == manifest["baseline"][rules.STATE_FILE]["mode"])
        if not already_applied:
            raise rules.ProjectError("项目规则、override 或状态在起草后已变化；保留草稿，未覆盖修改")
    else:
        state, state_bytes = rules._read_state(root)
        if state != manifest["state"] or state_bytes != contents[rules.STATE_FILE]:
            raise rules.ProjectError("草稿的项目状态快照与基线不一致")
        _validate_active(state, contents)
    previous = contents[manifest["file"]]
    diff = "".join(difflib.unified_diff(
        _utf8(previous, "项目指令").splitlines(keepends=True),
        _utf8(candidate, "候选规则").splitlines(keepends=True),
        fromfile=manifest["file"], tofile="candidate.md"))
    value = _prepared_value(draft, manifest)
    value.update(candidate_sha256=candidate_sha, diff=diff, already_applied=already_applied)
    value["diagnostics"] = (["候选规则超过约 32 KiB 的 Codex 默认指令上下文限制；完整加载尚未验证"]
                            if len(candidate) > 32768 else [])
    return value, candidate, expected_state, contents


def preview_edit(draft_dir):
    draft, root, manifest = _load_draft(draft_dir)
    with rules.project_lock(root):
        return _review_data(draft, root, manifest)[0]


def apply_edit(draft_dir, *, candidate_sha256):
    if not isinstance(candidate_sha256, str) or not rules.SHA256.fullmatch(candidate_sha256):
        raise rules.ProjectError("必须提供审查时获得的候选 SHA-256")
    draft, root, manifest = _load_draft(draft_dir)
    with rules.project_lock(root):
        value, candidate, state, contents = _review_data(draft, root, manifest)
        if value["candidate_sha256"] != candidate_sha256:
            raise rules.ProjectError("候选在审查后发生变化；请重新检查 diff，未应用")
        edits = [rules._instruction_edit(root, manifest["file"], contents[manifest["file"]], candidate),
                 rules._state_edit(root, contents[rules.STATE_FILE], state)]
        backup = rules._apply(root, edits, "edit")
    value.update(result="applied" if backup else "unchanged", backup=str(backup) if backup else None,
                 state_file=str(root / rules.STATE_FILE), enabled=True)
    return value


def _replace_draft_file(draft, name, data):
    target = draft / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise rules.ProjectError(f"草稿文件发生重定向：{target}")
    descriptor, temporary = tempfile.mkstemp(prefix=name + "-", dir=str(draft))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate_edit(prepared, codex_home, *, command_provider=None):
    """Generate a draft using the existing CLI worker; never apply it here."""
    if not isinstance(prepared, dict) or not prepared.get("draft_dir"):
        raise rules.ProjectError("必须提供 prepare_edit 返回的草稿")
    draft, root, manifest = _load_draft(prepared["draft_dir"])
    initial_view = preview_edit(draft)  # Confirm the baseline before a model request.
    prompt = _bounded_read(draft / "prompt.txt", 512 * 1024)
    if prompt is None or not prompt.strip():
        raise rules.ProjectError("草稿任务书不存在或为空")
    task = _utf8(prompt, "草稿任务书")
    import worker_runtime
    prefix = command_provider() if callable(command_provider) else command_provider
    runtime = worker_runtime.Runtime(codex_home=codex_home, command_prefix=prefix)
    snapshot = None
    try:
        worker = manifest["state"]["worker"]
        snapshot = runtime.spawn(task=task, cwd=str(root), model=worker["model"],
                                 effort=worker["reasoning_effort"], sandbox="read-only")
        while snapshot["status"] == "running":
            time.sleep(0.05)
            snapshot = runtime.get(snapshot["agent_id"])
        # Keep the tool's requested/observed metadata, without claiming physical
        # model identity. Failed or interrupted output is never a candidate.
        _replace_draft_file(draft, "generation.json", rules._json_bytes(snapshot))
        if snapshot["status"] != "completed" or snapshot.get("output_truncated"):
            raise rules.ProjectError(
                f"AI 生成失败（agent_id={snapshot['agent_id']}，status={snapshot['status']}）："
                + (snapshot.get("error") or snapshot.get("output_note") or "未生成完整文档"))
        raw = snapshot["output"].encode("utf-8")
        _candidate_details(raw, worker, b"\r\n" if manifest["newline"] == "crlf" else b"\n")
        # Another process can change project files while the AI is running.
        # Such output remains in generation.json, never becomes an applicable draft.
        current_view = preview_edit(draft)
        if current_view["candidate_sha256"] != initial_view["candidate_sha256"]:
            raise rules.ProjectError("候选在 AI 生成期间被修改；保留已有草稿，未替换候选")
        _replace_draft_file(draft, "candidate.md", raw)
        value = preview_edit(draft)
        value.update(result="generated", generation=str(draft / "generation.json"),
                     agent_id=snapshot["agent_id"], cli_session_id=snapshot.get("cli_session_id"),
                     requested=snapshot.get("requested"), observed=snapshot.get("observed"))
        return value
    except KeyboardInterrupt:
        if snapshot is not None and snapshot.get("status") == "running":
            cancelled = runtime.cancel(snapshot["agent_id"])
            _replace_draft_file(draft, "generation.json", rules._json_bytes(cancelled))
        raise
    except RuntimeError as exc:
        raise rules.ProjectError(f"无法通过现有 CLI 生成项目规则：{exc}") from exc
    finally:
        runtime.shutdown()
