#!/usr/bin/env python3
"""严格校验执行模型能力声明。"""

import json
import re
from pathlib import Path


EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
DEFAULT_CAPABILITIES = Path(__file__).resolve().parent.parent / "model-capabilities.json"


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


def check_model(value):
    if not isinstance(value, str) or not MODEL_NAME.fullmatch(value):
        raise ConfigError("执行模型 ID 不能为空，且必须使用有效的模型标识")
    return value


def check_effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ConfigError(f"思考强度无效，可用的配置值为：{', '.join(sorted(EFFORTS))}")
    return value


def _check_fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} 必须是 JSON 对象")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ConfigError(f"{label} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        names = sorted(str(key) for key in unknown)
        raise ConfigError(f"{label} 包含未知字段：{', '.join(names)}")


def validate_capabilities(value):
    """校验并复制一个完整的模型能力声明。"""
    _check_fields(value, {"version", "source", "models"}, set(), "capabilities")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ConfigError("能力声明格式版本 version 必须为 1")
    if not isinstance(value["source"], str) or not value["source"].strip():
        raise ConfigError("能力声明 source 必须是非空来源描述")
    if not isinstance(value["models"], dict) or not value["models"]:
        raise ConfigError("能力声明 models 必须是非空对象")

    models = {}
    for model, efforts in value["models"].items():
        check_model(model)
        if not isinstance(efforts, list) or not efforts:
            raise ConfigError(f"模型 {model!r} 的能力列表必须是非空数组")
        normalized_efforts = []
        seen = set()
        for effort in efforts:
            check_effort(effort)
            if effort in seen:
                raise ConfigError(f"模型 {model!r} 的能力列表不能有重复思考强度：{effort}")
            seen.add(effort)
            normalized_efforts.append(effort)
        models[model] = normalized_efforts
    return {"version": 1, "source": value["source"], "models": models}


def load_builtin_capabilities(path=None):
    """读取仓库随附的、带来源说明的能力快照。"""
    if path is None:
        path = DEFAULT_CAPABILITIES
    return validate_capabilities(read_json(path))
