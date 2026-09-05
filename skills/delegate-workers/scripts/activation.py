"""Plan reversible changes to the owned default-delegation instruction block."""

import hashlib
from pathlib import Path


BEGIN = b"\n<!-- delegate-workers:begin -->\n"
END = b"<!-- delegate-workers:end -->\n"
FILES = {"AGENTS.md", "AGENTS.override.md"}
MODES = {"auto", "on-demand"}


class ActivationError(ValueError):
    pass


def read_file(path):
    if path.is_symlink():
        raise ActivationError(f"指令文件是符号链接，未进行修改：{path}")
    return path.read_bytes() if path.exists() else None


def effective_file(home):
    override = home / "AGENTS.override.md"
    return override if (read_file(override) or b"").strip() else home / "AGENTS.md"


def validate_state(state):
    if not isinstance(state, dict) or state.get("mode") not in MODES:
        raise ActivationError("默认委派模式的安装记录无效")
    if state["mode"] == "auto":
        if (state.get("file") not in FILES or not isinstance(state.get("sha256"), str)
                or type(state.get("created_file")) is not bool):
            raise ActivationError("默认委派指令的安装记录无效")
    return state


def strip_owned(data, state, *, repair=False):
    content = data or b""
    if BEGIN not in content and END not in content and repair:
        return content
    if content.count(BEGIN) != 1 or content.count(END) != 1:
        raise ActivationError("默认委派指令缺失或标记不完整；请检查指令文件，再重新开启")
    start = content.index(BEGIN)
    end = content.index(END) + len(END)
    if end < start or hashlib.sha256(content[start:end]).hexdigest() != state["sha256"]:
        raise ActivationError("默认委派指令块被修改过，未覆盖修改；请先检查该指令块")
    return content[:start] + content[end:]


def plan(home, skill, previous, mode, template=None, *, repair=False):
    """Return metadata and path -> (before, after) edits; never write files."""
    previous = validate_state(previous or {"mode": "on-demand"})
    if mode not in MODES:
        raise ActivationError("模式必须是 auto（默认委派）或 on-demand（按需匹配）")
    contents = {}
    changed = {}
    if previous["mode"] == "auto":
        old_path = home / previous["file"]
        before = read_file(old_path)
        remainder = strip_owned(before, previous, repair=repair)
        contents[old_path] = before
        changed[old_path] = None if not remainder and previous["created_file"] else remainder
    state = {"mode": "on-demand"}
    if mode == "auto":
        target = effective_file(home)
        before = read_file(target)
        contents.setdefault(target, before)
        base = changed.get(target, before)
        if BEGIN in (base or b"") or END in (base or b""):
            raise ActivationError(f"指令文件中有未登记的委派指令块，未进行覆盖：{target}")
        if template is None:
            raise ActivationError("当前版本不包含默认委派规则，请先更新")
        rendered = template.replace("{skill_path}", str(skill / "SKILL.md")).encode("utf-8")
        block = BEGIN + rendered.rstrip(b"\n") + b"\n" + END
        changed[target] = (base or b"") + block
        state = {"mode": "auto", "file": target.name,
                 "sha256": hashlib.sha256(block).hexdigest(), "created_file": base is None}
    edits = {path: (contents[path], after) for path, after in changed.items()
             if contents[path] != after}
    return state, edits


def status(home, state):
    state = validate_state(state or {"mode": "on-demand"})
    result = {"mode": state["mode"], "rule_present": False, "issue": None}
    if state["mode"] == "on-demand":
        return result
    path = home / state["file"]
    result["file"] = str(path)
    try:
        data = read_file(path)
        strip_owned(data, state)
        if effective_file(home) != path:
            raise ActivationError("默认委派指令被 AGENTS.override.md 遮蔽，请重新开启默认委派以迁移规则")
        if len(data) > 32768:
            raise ActivationError("全局指令超过 Codex 默认的 32 KiB 限制，委派规则可能被截断")
        result["rule_present"] = True
    except (ValueError, OSError) as exc:
        result["issue"] = str(exc)
    return result
