# 执行模型配置

本项目只提供执行模型预设和静态兼容性预检。主代理的模型与思考强度始终保持不变；仓库默认配置仍使用 `medium`，不把某台机器的 Luna/max 偏好变成所有用户的长期规则。

## 持久配置

`skills/delegate-workers/workers.json` 的格式为：

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

预设是偏好而非模型白名单。`validate_config` 只做结构校验和 v1 → v2 迁移，不修改传入参数，也不做能力判断。旧版的并发、尝试次数和固定 fallback 字段不会继续生效；保存或更新时会保留可恢复备份。

## 项目级规则与全局模式

`workers.json`、`dw configure`、全局 `dw status`、`dw mode` 和 `dw uninstall` 管理的是本工具的全局安装与执行预设。全局模式的默认规则段不会自动注册项目，也不会因为当前任务位于某个仓库就修改项目文件。

项目级规则必须显式选择加入。安装完成后，在 Git 项目目录或其子目录中，最短初始化路径是：

```bash
dw project init
```

不需要填写 `--path`、`--model` 或 `--effort`，也不需要其它设置。工具会自动定位 Git 仓库 / worktree 根；新项目继承已安装配置的默认执行预设。已有项目不带参数重复 `init` 会继续使用项目状态中保存的执行快照，即使全局 `workers.json` 已变化。

非 Git 目录中的项目命令仍必须显式提供 `--path`；请在目标非 Git 目录中使用这一行：

```bash
dw project init --path .
```

### `init` 的可选参数

下面参数都是可选的定制项，不是初始化的必填项：

```bash
dw project init [--path PATH] [--profile NAME] [--model MODEL] [--effort EFFORT]
```

省略参数时，新 Git 项目使用已安装配置的默认 profile；`--profile NAME` 可选择另一个已配置预设，例如：

```bash
dw project init --profile complex
```

也可以显式覆盖模型和强度：

```bash
dw project init --model gpt-5.6-luna --effort max
```

`--model` 必须同时提供 `--effort`；`--effort` 可以单独覆盖强度。需要操作其它目录时显式使用 `--path`，例如：

```bash
dw project init --path /path/to/other-project --profile complex
```

仓库默认预设是 `gpt-5.6-luna` / `medium`；用户本机可以有不同的已安装配置，因此上面的 Luna/max 只是显式定制示例，不是所有用户的默认值。

### 项目文件、所有权与可逆变更

`init` 立即在选定项目根创建或合并正确的 `AGENTS.md`，保留现有内容并维护带标记的规则段；它不会创建错误拼写的 `agent.md`。如果项目根存在非空 `AGENTS.override.md`，该文件优先作为活动目标，工具会针对这个活动文件处理规则。所有权元数据写入项目根 `.delegate-workers-project.json`；用户编辑受管理段时，写入会停止而不会覆盖编辑，禁用保留可逆备份。

`.delegate-workers-project.json` 保存项目执行快照和受管理文件的所有权；分享或复制项目生命周期时，应与 `AGENTS.md`（或活动的 `AGENTS.override.md`）一起保留。`.delegate-workers-project.lock` 是项目根的稳定持久锁，操作期间不会删除；`.delegate-workers-project-backups/<timestamp-id>/manifest.json` 与其中的 preimage 文件保存变更前内容，供禁用和恢复使用。锁与备份通常不应提交到 Git；请按仓库策略自行忽略它们，本工具不会自动修改 `.gitignore`。

### `status`、`sync` 与 `disable`

```bash
dw project status [--path PATH]
dw project sync [--path PATH]
dw project disable [--path PATH]
```

非 Git 目录同样需要 `--path`；`sync` 使用已保存的项目执行快照，不会随全局配置变化而切换模型或强度。

`sync` 只作用于已经注册且启用的项目，且是用户显式调用的后续更新；没有后台持续重建、全局扫描或隐式选择加入。`status` 是只读诊断，可以指出从项目根到请求目录的嵌套规则存在潜在覆盖，但不证明规则的语义效果。`disable` 只停用一个项目规则，不能和全局 `dw mode on-demand` 或 `dw uninstall` 混用：后两者分别管理全局委派段和整个工具安装，不会代替项目生命周期操作。

### 状态与运行时边界

启用项目规则代表该项目明确选择“主代理规划和审查、执行工作交给指定执行模型”的分工；普通全局预设仍允许主代理按任务决定是否分派。项目规则不会强制声称已经执行：状态只分别标明规则的静态检查结果，并将会话加载、实际派发以及运行时 worker 身份保持为未验证或未知；其中 `session_loaded: null` 表示会话是否加载未知，不是证明活动会话没有加载，`runtime_verified: false` 也不证明没有实际运行。执行模型或强度不可用时，应报告不可用并询问替代，不要静默 fallback。

Codex 官方文档描述运行开始时的指令加载顺序：全局 `AGENTS.override.md` 优先于 `AGENTS.md`，项目规则从 Git 根到当前目录生效，较深目录优先，并有默认 32 KiB 总限制。[Codex AGENTS.md 文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md) 是加载关系的依据；本工具的状态检查只报告静态规则检查结果，不测量或承诺每个活动会话已经加载文件。

## 预检命令

```bash
python3 skills/delegate-workers/scripts/workers.py show
python3 skills/delegate-workers/scripts/workers.py validate
python3 skills/delegate-workers/scripts/workers.py resolve --profile complex
python3 skills/delegate-workers/scripts/workers.py resolve --model gpt-5.6-luna --effort max
```

`show` 返回纯结构配置，不检查能力；结构合法但不兼容的旧配置仍可显示，便于恢复。`validate` 检查所有 profile 的实际模型/强度组合并逐 profile 返回兼容性。`resolve` 只检查最终选中的组合，临时 `--model` / `--effort` 不写回配置；模型覆盖仍应同时给出 `--effort`。

兼容性信息的位置和范围如下：

- `resolve`：选中组合位于 `compatibility.status`，取值为 `compatible` 或 `unverified`。
- `validate`：每个 profile 位于 `compatibility.<profile>.status`，取值为 `compatible` 或 `unverified`。
- 两者都带有 `source`，且 `runtime_verified` 始终为 `false`，因为这是静态预检，不是运行时身份验证。
- `dw status` 是管理器的逐 profile 诊断，范围更宽，还可能报告 `incompatible` 或 `error`。

已知模型使用不支持的强度会拒绝。未知模型保持原值并标为 `unverified`、给出警告，不自动换模型、profile 或 fallback。`dw configure` 按内置快照检查新设置；`dw status` 逐 profile 展示诊断，人类菜单也展示诊断。已有不兼容配置可先用 `dw status` / `show` 查看，再修正强度后保存。

## 能力声明

内置快照取自 2026-09-09 本机 Codex native spawn 工具声明，不是官方模型目录，也不证明当前账户可用。它只记录：

- `gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`：`low`、`medium`、`high`、`xhigh`、`max`、`ultra`。
- `gpt-5.6-luna`：`low`、`medium`、`high`、`xhigh`、`max`。
- `gpt-5.5`：`low`、`medium`、`high`、`xhigh`。

需要使用调用者自己的完整声明时，通过全局 `--capabilities PATH` 传入，并放在子命令前：

```bash
python3 skills/delegate-workers/scripts/workers.py \
  --capabilities /path/to/capabilities.json validate
```

格式固定为：

```json
{
  "version": 1,
  "source": "caller-provided snapshot 2026-09-09",
  "models": {
    "exact-model-id": ["low", "max"]
  }
}
```

`source` 必须是非空描述，`models` 必须是非空对象，每个模型的强度列表必须非空且不重复。显式声明是完整集合，可以覆盖内置快照；其中没有的模型会拒绝，绝不回落内置快照。不要编造 catalog 只为通过检查。

## 生成 Codex 默认片段

`codex-config` 预检通过后只向 stdout 输出 `[agents]` 下的两个可选默认键：

```bash
python3 skills/delegate-workers/scripts/workers.py \
  codex-config --model gpt-5.6-luna --effort max
```

输出：

```toml
[agents]
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "max"
```

它不读取或写入全局 `config.toml`，不自动安装，也不修改主代理模型。若已有 `[agents]` 表，应审核后把这两个键合并进去，不要重复声明表。未知模型的警告写入 stderr，不污染 stdout 的 TOML 片段。官方文档将这两个字段定义为 spawned agents 的默认模型和默认思考强度，并说明显式 spawn 值优先：[OpenAI Subagents 全局设置](https://learn.chatgpt.com/docs/agent-configuration/subagents#global-settings)。因此它是可选原生默认值，不是硬锁，也不能保证所有客户端版本都支持。

更改模型或强度只影响执行请求；主代理仍负责规划、分工、审查和最终验收。用户明确指定的分工、固定模型/强度和文件范围优先于预设；不可用时应如实说明并询问替代，不应静默换 profile 或 fallback。
