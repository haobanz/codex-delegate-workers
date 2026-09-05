# 执行模型配置

本项目只提供执行模型预设。主代理决定任务拆分、模型选择、并行数量、重试和进一步分派；主代理自身的模型和思考强度沿用 Codex 会话设置。

```json
{
  "version": 2,
  "default_profile": "default",
  "profiles": {
    "default": {"model": "gpt-5.6-luna", "reasoning_effort": "medium"},
    "complex": {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
  }
}
```

可以添加或修改预设，并指定默认预设。预设是使用偏好，不是只能使用这些模型的白名单。实际可用模型和参数由当前 Codex 环境决定。

配置工具从它所在的技能目录读取 `workers.json`，也可显式指定另一份完整配置：

```bash
python3 skills/delegate-workers/scripts/workers.py show
python3 skills/delegate-workers/scripts/workers.py resolve --profile complex
python3 skills/delegate-workers/scripts/workers.py resolve --effort high
python3 skills/delegate-workers/scripts/workers.py --config /path/to/workers.json show
```

`resolve` 只返回所选模型和思考强度，不创建代理，不决定任务路由。`--model` 和 `--effort` 可临时覆盖预设，更改模型时应同时指定思考强度。临时覆盖不会写回配置。

新版兼容读取旧配置，保留所有预设名称、模型、思考强度和默认预设，忽略旧的并发数、尝试次数及固定后备角色字段。新版安装器或保存设置时会生成精简配置，原配置留在 `workers.json.bak` 及相应安装备份中。若从旧版 `dw update` 升级，旧格式可能暂时保留到下一次保存设置或更新，旧限制不会继续生效。

默认启用开关由 `dw mode auto` / `dw mode on-demand` 管理，不改变上述模型设置。它让 Codex 在需要分派时知道这些偏好，不要求每项工作都创建子代理。子代理可以继续分派，实际行为由模型判断和运行环境决定。
