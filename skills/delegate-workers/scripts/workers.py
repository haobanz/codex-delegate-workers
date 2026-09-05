#!/usr/bin/env python3
"""Validate worker-only settings and produce native subagent dispatch plans."""

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
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=unique_object)
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


def check_int(value, label, minimum):
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{label} 必须是大于或等于 {minimum} 的整数")
    return value


def check_model(value):
    if not isinstance(value, str) or not MODEL_NAME.fullmatch(value):
        raise ConfigError("执行模型 ID 不能为空，且必须使用有效的模型标识")
    return value


def check_effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ConfigError(f"思考强度无效，可用的配置值为：{', '.join(sorted(EFFORTS))}")
    return value


def validate_config(config):
    check_fields(config, {"version", "default_profile", "max_parallel_workers",
                          "max_attempts_per_task", "profiles"}, set(), "config")
    if type(config["version"]) is not int or config["version"] != 1:
        raise ConfigError("配置格式版本 version 必须为 1")
    check_int(config["max_parallel_workers"], "max_parallel_workers", 1)
    check_int(config["max_attempts_per_task"], "max_attempts_per_task", 1)
    profiles = config["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError("执行角色 profiles 必须是非空对象")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
            raise ConfigError(f"角色名称无效：{name!r}；请使用小写英文字母开头，可含数字、下划线或连字符，最多 64 个字符")
        check_fields(profile, {"model", "reasoning_effort"}, {"fallback"}, name)
        check_model(profile["model"])
        check_effort(profile["reasoning_effort"])
        fallback = profile.get("fallback")
        if fallback is not None and (not isinstance(fallback, str) or fallback not in profiles):
            raise ConfigError(f"{name} 的后备角色必须已存在，或设置为 null 关闭升级")
    default = config["default_profile"]
    if not isinstance(default, str) or default not in profiles:
        raise ConfigError("默认执行角色 default_profile 必须指向已有角色")
    for name in profiles:
        visited = set()
        current = name
        while current is not None:
            if current in visited:
                raise ConfigError(f"后备角色形成循环，请修改角色 {current} 的升级路径")
            visited.add(current)
            current = profiles[current].get("fallback")
    return config


def validate_capabilities(capabilities):
    check_fields(capabilities, {"models"}, set(), "capabilities")
    if not isinstance(capabilities["models"], list):
        raise ConfigError("模型能力列表 capabilities.models 必须是数组")
    models = {}
    for item in capabilities["models"]:
        check_fields(item, {"model", "reasoning_efforts"}, set(), "capability")
        model = check_model(item["model"])
        if model in models:
            raise ConfigError(f"模型能力列表中存在重复模型：{model}")
        efforts = item["reasoning_efforts"]
        if not isinstance(efforts, list):
            raise ConfigError("模型支持的思考强度 reasoning_efforts 必须是数组")
        models[model] = {check_effort(effort) for effort in efforts}
    return models


def resolve(config, *, profile=None, model=None, effort=None, escalate_from=None,
            attempt=1, active_workers=0, available_slots=None, max_parallel=None,
            capabilities=None):
    validate_config(config)
    check_int(attempt, "attempt", 1)
    check_int(active_workers, "active_workers", 0)
    if available_slots is not None:
        check_int(available_slots, "available_slots", 0)
    limit = config["max_parallel_workers"] if max_parallel is None else max_parallel
    check_int(limit, "max_parallel", 1)
    if model is not None:
        check_model(model)
        if effort is None:
            raise ConfigError("临时更改执行模型时，必须同时指定思考强度 --effort")
    if effort is not None:
        check_effort(effort)
    if escalate_from is not None and any(value is not None for value in (profile, model, effort)):
        raise ConfigError("--escalate-from 不能与 --profile、--model 或 --effort 同时使用")
    if escalate_from is not None and attempt < 2:
        raise ConfigError("失败升级需要已有一次尝试，请设置 --attempt 为 2 或更大值")
    known_models = None if capabilities is None else validate_capabilities(capabilities)
    profiles = config["profiles"]
    selected = escalate_from if escalate_from is not None else profile
    if selected is None:
        selected = config["default_profile"]
    if not isinstance(selected, str) or selected not in profiles:
        raise ConfigError(f"执行角色不存在：{selected!r}")
    result = {
        "action": "return_to_parent",
        "main_session": "unchanged",
        "attempt": attempt,
        "limits": {"max_parallel_workers": limit,
                   "max_attempts_per_task": config["max_attempts_per_task"]},
        "execution": "not_started",
    }
    if attempt > config["max_attempts_per_task"]:
        return {**result, "reason": "attempt_limit_reached"}
    if escalate_from is not None:
        selected = profiles[selected].get("fallback")
        if selected is None:
            return {**result, "reason": "no_fallback_configured"}
    worker = profiles[selected]
    request = {
        "model": worker["model"] if model is None else model,
        "reasoning_effort": worker["reasoning_effort"] if effort is None else effort,
    }
    result.update(profile=selected, worker_request=request,
                  availability="unverified" if known_models is None else "supported")
    if known_models is not None:
        if request["model"] not in known_models:
            return {**result, "availability": "unsupported", "reason": "model_unavailable"}
        if request["reasoning_effort"] not in known_models[request["model"]]:
            return {**result, "availability": "unsupported", "reason": "effort_unavailable"}
    if active_workers >= limit or available_slots == 0:
        return {**result, "action": "wait", "reason": "concurrency_limit_reached"}
    return {**result, "action": "delegate",
            "reason": "configured_fallback" if escalate_from is not None else "selected_worker"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate worker-only configuration")
    commands.add_parser("show", help="Show the validated configuration")
    dispatch = commands.add_parser("resolve", help="Plan a worker request without executing it")
    dispatch.add_argument("--profile")
    dispatch.add_argument("--model")
    dispatch.add_argument("--effort")
    dispatch.add_argument("--escalate-from")
    dispatch.add_argument("--attempt", type=int, default=1)
    dispatch.add_argument("--active-workers", type=int, default=0)
    dispatch.add_argument("--available-slots", type=int)
    dispatch.add_argument("--max-parallel", type=int)
    dispatch.add_argument("--capabilities", type=Path)
    args = parser.parse_args(argv)
    try:
        config = validate_config(read_json(args.config))
        if args.command == "validate":
            output = {"valid": True, "profiles": list(config["profiles"]),
                      "main_session": "unchanged"}
        elif args.command == "show":
            output = config
        else:
            output = resolve(
                config, profile=args.profile, model=args.model, effort=args.effort,
                escalate_from=args.escalate_from, attempt=args.attempt,
                active_workers=args.active_workers, available_slots=args.available_slots,
                max_parallel=args.max_parallel,
                capabilities=None if args.capabilities is None else read_json(args.capabilities),
            )
        print(json.dumps(output, indent=2, ensure_ascii=True))
        return 0
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
