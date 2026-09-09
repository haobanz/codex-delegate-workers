#!/usr/bin/env python3
"""读取执行模型预设；任务分配由当前 Codex 主代理决定。"""

import argparse
import json
import re
import sys
from pathlib import Path

import capabilities as capability_schema
import platform_support


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "workers.json"
DEFAULT_CAPABILITIES = capability_schema.DEFAULT_CAPABILITIES
EFFORTS = capability_schema.EFFORTS
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MODEL_NAME = capability_schema.MODEL_NAME


ConfigError = capability_schema.ConfigError
unique_object = capability_schema.unique_object
read_json = capability_schema.read_json
check_model = capability_schema.check_model
check_effort = capability_schema.check_effort
validate_capabilities = capability_schema.validate_capabilities


def check_fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} 必须是 JSON 对象")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ConfigError(f"{label} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        names = sorted(str(key) for key in unknown)
        raise ConfigError(f"{label} 包含未知字段：{', '.join(names)}")


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


def _load_builtin_capabilities():
    return capability_schema.load_builtin_capabilities(DEFAULT_CAPABILITIES)


def _validate_worker_against_catalog(worker, catalog, builtin):
    check_fields(worker, {"model", "reasoning_effort"}, set(), "worker")
    model = check_model(worker["model"])
    effort = check_effort(worker["reasoning_effort"])
    source = catalog["source"]
    supported = catalog["models"].get(model)
    if supported is None:
        if not builtin:
            raise ConfigError(f"显式能力声明未包含模型：{model}")
        return {
            "status": "unverified",
            "source": source,
            "runtime_verified": False,
            "warning": f"模型 {model!r} 不在能力快照中，运行时支持未验证；不会自动降级或替换。",
        }
    if effort not in supported:
        raise ConfigError(f"模型 {model!r} 不支持思考强度 {effort!r}；能力声明支持：{', '.join(supported)}")
    return {"status": "compatible", "source": source, "runtime_verified": False}


def validate_worker(worker, capabilities=None):
    """预检最终 worker 组合，并返回不声称运行时探测的兼容性信息."""
    builtin = capabilities is None
    catalog = _load_builtin_capabilities() if builtin else validate_capabilities(capabilities)
    return _validate_worker_against_catalog(worker, catalog, builtin)


def resolve(config, *, profile=None, model=None, effort=None, capabilities=None):
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
    compatibility = validate_worker(worker, capabilities)
    return {"profile": selected, "worker_request": worker,
            "main_session": "unchanged", "execution": "not_started",
            "compatibility": compatibility}


def _add_selection_arguments(command):
    command.add_argument("--profile")
    command.add_argument("--model")
    command.add_argument("--effort")


def _emit_warning(compatibility):
    if compatibility.get("warning"):
        print(f"警告：{compatibility['warning']}", file=sys.stderr)


def _load_capabilities(path):
    if path is None:
        return None
    return validate_capabilities(read_json(path))


def codex_config_fragment(worker):
    return ("[agents]\n"
            f"default_subagent_model = {json.dumps(worker['model'], ensure_ascii=False)}\n"
            f"default_subagent_reasoning_effort = {json.dumps(worker['reasoning_effort'], ensure_ascii=False)}\n")


def main(argv=None):
    platform_support.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--capabilities", type=Path,
                        help="使用调用者提供的完整模型能力声明 JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="检查模型预设格式及组合兼容性",
                        description="检查模型预设格式及组合兼容性")
    commands.add_parser("show", help="显示模型预设")
    selection = commands.add_parser("resolve", help="读取指定预设或临时覆盖参数")
    _add_selection_arguments(selection)
    codex_config = commands.add_parser("codex-config", help="输出 Codex agents TOML 片段")
    _add_selection_arguments(codex_config)
    args = parser.parse_args(argv)
    try:
        config = validate_config(read_json(args.config))
        if args.command == "show":
            output = config
        elif args.command == "validate":
            builtin = args.capabilities is None
            catalog = _load_builtin_capabilities() if builtin else _load_capabilities(args.capabilities)
            compatibilities = {
                name: _validate_worker_against_catalog(profile, catalog, builtin)
                for name, profile in config["profiles"].items()
            }
            output = {"valid": True, "profiles": list(config["profiles"]),
                      "compatibility": compatibilities, "main_session": "unchanged"}
        elif args.command == "codex-config":
            catalog = _load_capabilities(args.capabilities) if args.capabilities is not None else None
            resolved = resolve(config, profile=args.profile, model=args.model, effort=args.effort,
                               capabilities=catalog)
            _emit_warning(resolved["compatibility"])
            print(codex_config_fragment(resolved["worker_request"]), end="")
            return 0
        else:
            catalog = _load_capabilities(args.capabilities) if args.capabilities is not None else None
            output = resolve(config, profile=args.profile, model=args.model, effort=args.effort,
                             capabilities=catalog)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        if args.command == "validate":
            for compatibility in output["compatibility"].values():
                _emit_warning(compatibility)
        elif args.command == "resolve":
            _emit_warning(output["compatibility"])
        return 0
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
