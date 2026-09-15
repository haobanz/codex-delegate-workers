#!/usr/bin/env python3
"""定位本机真实 Codex CLI 可执行文件，供统一 worker 接口启动独立进程。"""

import os
import shutil
import sys
from pathlib import Path

import platform_support


class CliNotFoundError(ValueError):
    """找不到可用的 Codex CLI 可执行文件。"""


def _is_usable_file(path):
    path = Path(path)
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _posix_candidates():
    candidates = []
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    home = Path.home()
    candidates.append(home / ".local/bin/codex")
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    candidates.append(codex_home / "bin/codex")
    candidates.append(codex_home.parent / "bin/codex")
    candidates.extend(sorted((codex_home / "packages").glob("*/current/bin/codex")))
    candidates.append(Path("/usr/local/bin/codex"))
    candidates.append(Path("/opt/homebrew/bin/codex"))
    return candidates


def _node_executable():
    found = shutil.which("node")
    return Path(found).resolve() if found else None


def _is_node_script(path):
    """npm 生成的入口通常没有 .js 后缀，只能读 shebang 判断。"""
    if Path(path).suffix == ".js":
        return True
    try:
        with open(path, "rb") as stream:
            first = stream.readline(256)
    except OSError:
        return False
    return b"node" in first and first.startswith(b"#!")


def _resolve_posix():
    checked = []
    javascript = []
    for candidate in _posix_candidates():
        if not candidate:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        checked.append(str(candidate))
        if _is_usable_file(resolved):
            if _is_node_script(resolved):
                # npm 安装的入口是 Node 脚本：用 Node 执行，不依赖 shebang 与 PATH。
                javascript.append(resolved)
                continue
            return [str(resolved)]
    if javascript:
        node = _node_executable()
        if node is not None:
            return [str(node), str(javascript[0])]
        raise CliNotFoundError(
            f"检测到 npm 安装的 Codex 入口 {javascript[0]}，但 PATH 中没有 node，无法启动。"
            "请安装 Node.js，或改用官方原生 Codex 可执行文件。")
    raise CliNotFoundError(
        "未找到可用的 Codex CLI 可执行文件。请先安装 Codex CLI（例如 npm install -g @openai/codex），"
        "确保 codex 位于 PATH 中，或设置 CODEX_HOME 指向当前 Codex 安装位置。已查找：" + "、".join(checked[:8]))


def _windows_candidates():
    candidates = []
    for name in ("codex.exe", "codex.cmd", "codex.bat", "codex"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    for name in ("codex.exe", "codex.cmd"):
        candidates.append(local / "Programs/codex" / name)
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    candidates.append(appdata / "npm/codex.cmd")
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    candidates.append(codex_home / "bin/codex.exe")
    candidates.extend(sorted((codex_home / "packages").glob("*/current/bin/codex.exe")))
    return candidates


def _windows_fallback_executables():
    """已知的 Node 安装位置，用于直接执行 npm 安装的 codex.js。"""
    executables = []
    for name in ("node.exe", "node"):
        found = shutil.which(name)
        if found:
            executables.append(Path(found))
    program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
    executables.append(program_files / "nodejs/node.exe")
    return executables


def _windows_codex_js(cmd_path):
    """从 npm 生成的 codex.cmd 旁找到真实 codex.js，不通过 shell 传递参数。"""
    base = Path(cmd_path).parent
    candidates = [
        base / "node_modules/@openai/codex/bin/codex.js",
        base / "../node_modules/@openai/codex/bin/codex.js",
        base / "../lib/node_modules/@openai/codex/bin/codex.js",
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _resolve_windows():
    shims = []
    checked = []
    for candidate in _windows_candidates():
        if not candidate:
            continue
        checked.append(str(candidate))
        suffix = candidate.suffix.lower()
        if suffix == ".exe" and candidate.is_file():
            return [str(candidate)]
        if suffix in {".cmd", ".bat", ".ps1", ""} and candidate.is_file():
            shims.append(candidate)
    for shim in shims:
        if shim.suffix.lower() != ".cmd":
            continue
        script = _windows_codex_js(shim)
        if script is None:
            continue
        for executable in _windows_fallback_executables():
            if executable.is_file():
                return [str(executable), str(script)]
    if shims:
        raise CliNotFoundError(
            "检测到 Codex 命令包装脚本但无法安全启动：npm 生成的 .cmd 包装不能把用户参数交给 shell，"
            "也无法定位可用的 Node.js。请安装 Node.js，或改用官方原生 codex.exe。包装脚本："
            + "、".join(str(item) for item in shims))
    raise CliNotFoundError(
        "未找到可用的 Codex CLI 可执行文件。请安装 Codex CLI 并确保 codex 位于 PATH 中。已查找："
        + "、".join(checked[:8]))


def codex_command(path=None):
    """返回本机真实 Codex CLI 的命令前缀（参数数组，不经过 shell）。"""
    if path is not None:
        candidate = Path(path).expanduser()
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            raise CliNotFoundError(f"指定的 Codex CLI 路径不可执行：{candidate}")
        return [str(candidate)]
    if platform_support.WINDOWS:
        return _resolve_windows()
    return _resolve_posix()


def main(argv=None):  # pragma: no cover - 供人工排查
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path)
    args = parser.parse_args(argv)
    try:
        print(" ".join(codex_command(args.codex)))
    except (CliNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
