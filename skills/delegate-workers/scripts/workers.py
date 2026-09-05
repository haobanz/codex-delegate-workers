#!/usr/bin/env python3
"""读取执行模型预设；任务分配由当前 Codex 主代理决定。"""

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "workers.json"
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")


class ConfigError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"JSON 中有重复字段：{key}")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取 JSON 配置 {path}：{exc}") from exc


def check_fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} 必须是 JSON 对象")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ConfigError(f"{label} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"{label} 包含未知字段：{', '.join(sorted(unknown))}")


def check_model(value):
    if not isinstance(value, str) or not MODEL_NAME.fullmatch(value):
        raise ConfigError("执行模型 ID 不能为空，且必须使用有效的模型标识")
    return value


def check_effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ConfigError(f"思考强度无效，可用的配置值为：{', '.join(sorted(EFFORTS))}")
    return value


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] not in {1, 2}:
        raise ConfigError("配置格式版本 version 必须为 1 或 2")
    legacy = config["version"] == 1
    check_fields(config, {"version", "default_profile", "profiles"},
                 {"max_parallel_workers", "max_attempts_per_task"} if legacy else set(), "config")
    if not isinstance(config["profiles"], dict) or not config["profiles"]:
        raise ConfigError("执行模型预设 profiles 必须是非空对象")
    profiles = {}
    for name, profile in config["profiles"].items():
        if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
            raise ConfigError(f"预设名称无效：{name!r}；请使用小写英文字母开头，可含数字、下划线或连字符")
        check_fields(profile, {"model", "reasoning_effort"}, {"fallback"} if legacy else set(), name)
        profiles[name] = {"model": check_model(profile["model"]),
                          "reasoning_effort": check_effort(profile["reasoning_effort"])}
    default = config["default_profile"]
    if not isinstance(default, str) or default not in profiles:
        raise ConfigError("默认执行预设 default_profile 必须指向已有预设")
    # Version 1 constraints are intentionally omitted during model-only migration.
    return {"version": 2, "default_profile": default, "profiles": profiles}


def resolve(config, *, profile=None, model=None, effort=None):
    config = validate_config(config)
    selected = config["default_profile"] if profile is None else profile
    if not isinstance(selected, str) or selected not in config["profiles"]:
        raise ConfigError(f"执行预设不存在：{selected!r}")
    worker = dict(config["profiles"][selected])
    if model is not None:
        if effort is None:
            raise ConfigError("更改模型时请同时指定思考强度 --effort，避免沿用不兼容的档位")
        worker["model"] = check_model(model)
    if effort is not None:
        worker["reasoning_effort"] = check_effort(effort)
    return {"profile": selected, "worker_request": worker,
            "main_session": "unchanged", "execution": "not_started"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="检查模型预设格式")
    commands.add_parser("show", help="显示模型预设")
    selection = commands.add_parser("resolve", help="读取指定预设或临时覆盖参数")
    selection.add_argument("--profile")
    selection.add_argument("--model")
    selection.add_argument("--effort")
    args = parser.parse_args(argv)
    try:
        config = validate_config(read_json(args.config))
        if args.command == "validate":
            output = {"valid": True, "profiles": list(config["profiles"]), "main_session": "unchanged"}
        elif args.command == "show":
            output = config
        else:
            output = resolve(config, profile=args.profile, model=args.model, effort=args.effort)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
