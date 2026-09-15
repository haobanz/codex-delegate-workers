# 执行模型配置

本项目提供执行模型预设、静态兼容性预检，以及可选的一次性 MCP 统一接口。主代理的模型与思考强度始终保持不变；仓库默认配置仍使用 `medium`，不把某台机器的 Luna/max 偏好变成所有用户的长期规则。

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

## 续接、恢复与运行记录

执行模型和思考强度适用于 worker 的后续轮次，不能仅凭首次创建时显式传参就声称全程一致。`resume_agent`、`send_input` 等名称只是可能的宿主工具名称，执行时必须读取当前工具的实际参数和生命周期语义。

| 状态 | 处理方式 |
| --- | --- |
| 完成一轮但仍存活或空闲 | 宿主保持配置时正常续接；有元数据则核对新轮次，不因完成一轮机械重建。 |
| 已关闭，恢复路径只接受 ID 或无法证明保留参数 | 使用明确的 `model` 和 `reasoning_effort` 新建 worker，交接文件范围、已有改动、剩余任务及测试结果。 |
| 恢复路径能明确设置/保留所选参数并提供新证据 | 可以使用该路径，核对恢复后的轮次；需要先启动才能获得记录时，先交仅检查、不改文件的任务。 |
| 观察到模型或强度不匹配 | 停止派活并中断或关闭旧 worker；确认已停止后再给替代 worker 相同文件的写入任务。保留改动供审查，不自动删除或归为指定模型的成果。 |
| 没有新元数据或只有旧记录 | 标为未验证，不能以消息里的模型名、相同 ID 或静态预检通过代替。用户要求先核验后执行但宿主无法提供时，说明限制并询问替代方案。 |

有本地会话记录时，只读取相关 worker 的 `turn_context` 等必要元数据，关联真实 agent ID、轮次/时间以及模型和思考强度。核对首次与后续记录，不能只看最后一次匹配而遗漏中间的变化。不要把后台全量扫描、任务台账或固定轮询作为普通委派的前提。需要保存核对结果时放在当前项目 `tmp/`，不要把其他项目的会话内容写入仓库。

发送“继续用 Luna/max”等文字不会切换模型；反复 `send_input` 也不是修复方式。主代理接手的实现、指定 worker 的执行以及参数不匹配期间的工作要分别说明。会话记录表明的是记录的模型配置，并不独立验证自定义供应商上游的物理模型身份。主模型、全局默认配置和既有任务分配自主权不因这条规范而改变。

这是针对一次本地恢复后记录发生变化的审计所作的指令修正；没有据此认定所有 Codex 客户端版本都存在同样行为，也没有修改宿主恢复代码。安装更新会刷新已开启的全局默认规则；已初始化的项目需显式运行 `dw project sync`，然后重新打开会话。`dw status` 仍是静态诊断，不会声称已核验正在运行的 worker。

## 统一接口

`0.7.0` 增加一个本地 MCP stdio 服务器，服务器名 `delegate-workers`，只使用 Python 标准库，不依赖网络框架或第三方包。它在本工具自己的命名空间暴露五个工具，不修改宿主内建 `collaboration.spawn_agent` 的模型枚举，也不把自己伪装成原生 agent：自定义模型 ID 原样转发给独立启动的 `codex exec` 进程，沿用调用者现有的供应商与认证，并显式传入所请求的思考强度。不需要额外的 API Key、settings 或模型目录，主 Codex 会话的模型和强度保持不变。

### 启用、状态与关闭

```bash
dw interface enable   # 一次性注册本工具自己的 MCP 条目，已启用时幂等
dw interface status   # 只读查看注册状态，不修改配置
dw interface disable  # 只移除本工具拥有的注册
```

菜单中的第 **7** 项（统一接口）提供同样的启用 / 查看 / 关闭操作。注册通过本机 Codex 的 `codex mcp add` 完成，只写入本工具注册的那一个 `delegate-workers` MCP 条目；普通安装和更新不会改动 `config.toml`，已有的模型预设和全局规则保持不变。启用、关闭接口和卸载时只新增、更新或移除本工具拥有的那个条目；由于写入方是本机 Codex CLI，它可能重新格式化 `config.toml`，但主模型、思考强度和其它无关设置会被保留，同名但非本工具管理的配置不会被覆盖或移除。注册完成后需要重启 Codex 或新建会话，宿主才会加载并发现这些工具；接口不承诺在当前会话中注入工具，工具是否真的出现在上下文里仍取决于宿主加载。接口注册不是自动委派开关，`dw mode` 与项目规则仍独立管理。

### 工具与参数

| 工具 | 必填参数 | 可选参数 | 作用 |
| --- | --- | --- | --- |
| `list_models` | `cwd` | — | 返回本机已配置的执行偏好、当前生效来源与静态兼容性 |
| `spawn_agent` | `task`、`cwd` | `profile`、`model`、`reasoning_effort`、`sandbox`、`writable_files` | 启动一个独立执行进程并立即返回 agent 标识 |
| `get_agent` | `agent_id` | — | 查询单个任务的状态，完成后返回最终结果与证据路径 |
| `list_agents` | — | — | 列出本接口管理的任务 |
| `cancel_agent` | `agent_id` | — | 真正停止该任务及其子进程树后再报告终态 |

- `cwd` 必须是绝对路径、已经存在且是目录；它决定后端进程的工作目录和项目 `tmp/` 的归属。
- `sandbox` 取 `read-only`（默认）或 `workspace-write`；只有任务确实需要修改代码或文件时才使用 `workspace-write`。
- `writable_files` 是传给执行者的文件范围说明，不是每文件级别的强制 ACL；它用于沟通边界，执行仍受所选的沙箱模式约束。
- `list_models` 只报告调用者已配置的偏好，不是供应商完整模型目录，也不代表账户可用性。

最小 `spawn_agent` 调用示例（模型与强度只是示例，不代表所有用户或所有任务的默认值；`cwd` 请替换为真实的绝对路径）：

```json
{
  "task": "实现这个功能，并运行相关测试；只改授权范围内的文件。",
  "cwd": "/absolute/path/to/your/project",
  "model": "deepseek/deepseek-v4.1-flash",
  "reasoning_effort": "max",
  "sandbox": "workspace-write",
  "writable_files": ["src/module.py", "tests/test_module.py"]
}
```

`model` 需要同时提供 `reasoning_effort`。省略 `model` 时才按预设或项目快照解析；显式的 `profile`、`model` 和 `reasoning_effort` 只在本次调用内生效，不写回 `workers.json`、项目快照或全局配置。

### 偏好解析

每次调用都实时读取已安装的 `workers.json`，不缓存旧副本；切换安装目录时读取对应 `CODEX_HOME` 下的配置。默认选择顺序：

1. 已启用、结构有效且规则块完整无损的项目快照优先；
2. 没有可用项目快照（未注册或已禁用）时使用全局默认 profile；
3. 显式传入的 `profile` 或 `model` + `reasoning_effort` 在当前用户授权范围内覆盖以上选择。

已启用但快照损坏或规则块被改动的项目会报错，而不是静默回落到全局默认。未知自定义模型原样保留并标为 `unverified`，已知模型使用不支持的强度会被拒绝；接口不做自动换模型、profile 或 fallback，也不会编造能力数据来通过检查。

### 返回含义与状态

- 工具返回的标识是本接口生成的 `agent_id`，与后端 CLI 会话 ID 分开报告：只有 CLI 启动时真的给出了会话 ID，才会一并返回；不要把父会话或借用的 ID 当作 worker 的。
- 请求参数与观察到的启动配置分开记录：请求的模型/强度表示这次调用要求什么；观察值来自 CLI 启动头部，表示后端记录了什么。两者都不是对上游供应商物理模型身份的证明。
- 缺少运行时元数据时标为未验证；观察到的模型或强度与请求不一致时，接口会停止该任务并报失败，不继续派发工作。
- 任务状态为 `running`、`completed`、`failed` 或 `cancelled`。`completed` 表示子进程正常结束并产出最终消息；失败会带可读原因和日志路径。
- 当前是 MVP，没有 resume 工具：下一个任务用新的显式 `spawn_agent` 并交接已有改动与剩余工作。这只避免隐式恢复路径，不保证运行时不会漂移；参数一致性仍以宿主为每个新轮次提供的可用元数据为准，缺失时标为未验证。

### 生命周期与输出

任务启动后立即返回，长任务通过 `get_agent` 轮询。取消会先终止子进程及其子进程树，确认停止后才报告终态；已经正常完成的任务即使随后收到取消，也保留真实的 `completed` / `failed` 结果。MCP 服务器断开时会取消它拥有的运行中任务；服务器重启不会自动恢复旧任务。

每次派发都是全新的独立进程，只有一个终态，没有自动重试、fallback 或主代理自动接手；并行数量和是否需要重试由调用者决定，接口不设固定并发上限，也不禁止有授权的进一步分工。提示、输出和日志只写在项目根的 `tmp/delegate-worker-*` 目录；MCP 服务器自身的 stdout 只承载协议消息，不混入日志。

### 注册的所有权、更新与回滚

- 更新保留已启用的接口及其安装路径，不需要重新注册。
- 卸载会先移除本工具拥有的匹配条目，再删除已安装代码；同名但非本工具管理的配置不会被覆盖或移除。
- 回滚到没有统一接口代码的旧版本前，需要先运行 `dw interface disable`；否则回滚会在改动前明确报错，避免留下指向已删除脚本的注册。
- 接口只转发原样的自定义模型 ID：不创建冒充原生模型的别名，也不修改宿主内建工具的模型枚举。

Linux、macOS、WSL 与 Windows 的接口代码按同一实现设计，但真实供应商端到端行为和各平台运行结果会随本机 CLI 版本变化；本仓库的模型预设、预检、项目规则和 `tmp/` 规范继续适用，统一接口只是同一个 `workers.json` 之上的执行通道。工具是否在当前会话可见取决于宿主加载；独立 CLI 工具连接和记录到的配置都不等于上游物理模型身份。

## 派发可用性与本地 CLI 路由

原生派发能接受当前选择时继续用它，它仍然是允许的路径。原生派发无法接受该组合、而当前会话又能看到统一接口的工具时，用上一节的工具执行：先用 `list_models` 确认已配置偏好和生效来源，再用 `spawn_agent` / `get_agent` / `cancel_agent`。接口也不可用时才使用下面的本地 CLI 直连路径。这是候选路径的区分，不是固定的重试或 fallback 链。

在声称“无法派发”之前，先检查当前启用的工具 schema / 清单和可用的文档化发现路径，不要把不同情况混为一谈：

- 派发工具确实不存在；
- 该接口存在但拒绝了指定模型；
- 供应商、认证或网络错误；
- 缺少运行时身份元数据；
- 某个业务工具（表格、浏览器连接等）断开。

任何一项都不推断另一项。过期的运行时快照是某一时刻的本地观测，不是对某模型的账户级否定，也不是“宿主没有调度工具”的证据。已经被拒绝或已经失败的派发也属于一种事实，不能在换成本地 CLI 直连后假定成功：先确认现有 agent 的状态和失败原因，再决定是否新建派发；同样的供应商失败不会因为换路径就自动消失，也不需要重试、替换 worker 或额外授权。原生 spawn 接口和本地 CLI 是两条独立的本地通道，一侧拒绝不代表另一侧不可用，但另一侧是否接受同样的模型和强度取决于已安装的 CLI 版本和供应商支持。

模型和强度以当前用户或项目选择为准，之前工作里引用的示例不覆盖当前选择。原生派发无法接受目标组合时，本机已可用的 Codex CLI 仍是既有授权范围内可以尝试的执行途径；能否用用户的既有供应商和认证运行完全相同的模型和强度，取决于本机 CLI 版本和供应商支持，用前先核对该 CLI 的真实 help 与功能。下面的非交互命令是一个具体示例（示例值 `deepseek/deepseek-v4.1-flash` / `max` 不是默认值，也不是预检或配置里写死的值）；文档示例基于本机 `codex-cli 0.154.0` 的 `codex exec`，其它版本请以实际 `--help` 为准：

```bash
codex exec --ephemeral --sandbox read-only \
  --model 'deepseek/deepseek-v4.1-flash' \
  -c 'model_reasoning_effort="max"' \
  '仅检查：确认这条命令可用并报告环境信息，不要修改任何文件。'
```

该命令会读取所选 `CODEX_HOME`（默认 `~/.codex`）下的 `config.toml`，继承其中配置的供应商和认证，不强制指定任意 provider，也不索取 API Key、不为这条路径安装组件；它不修改主模型、思考强度或其它全局设置。真实业务任务可以在既有授权内使用更合适的沙箱。需要保存的提示、日志或输出文件放在当前项目 `tmp/`，必要时只为该命令设置 `TMPDIR`、`TMP`、`TEMP` 指向该目录；不要使用系统临时目录。向子进程显式传入所请求的模型和强度，主会话的模型和强度保持不变。

独立 CLI worker 是独立进程，不是原生 agent：它会获得自己的 session id（如果该路径返回）；报告时不要把父会话或借用的 id 当作 worker 的。会话头部记录的是所请求的配置，一次成功响应说明路径可用；两者都不证明上游物理模型身份。另外要确认该路径具备任务需要的工具和文件，CLI worker 可能没有主会话拥有的业务工具连接（例如表格或浏览器集成），工具可见性不能靠改写指令来编造或承诺；依赖重试或分批传输读取的任务要核实预期覆盖是否完整，这条要求保持通用，不写死记录范围或分块大小，也不声称修复上游工具。

已有明确分工或适用项目规则要求特定模型/强度、而没有任何已授权途径能执行时，只暂停被阻塞的执行，说明具体限制并询问替代方案。主代理可以继续规划和做定位阻塞所需的最小诊断，但不得以“只读调查”“重试”或“fallback”等名义代做被分配的业务任务；披露限制不等于授权接手，分别报告请求与观察到的参数、以及主代理接手的工作，本身也不构成接手授权。既有的主代理接手明确批准可直接使用，无需重复索取。上文“可以继续独立工作”指已授权分工范围内的工作，不包括被阻塞的任务。这条路径是可选手段，不是强制调度器或所有任务的默认要求；全局默认预设仍保留主代理的一般自主权，也不引入固定重试、并发上限、审计台账或新的能力文件。

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
