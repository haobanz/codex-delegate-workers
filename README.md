# Delegate Workers

在 Codex 中设置执行子代理的模型和思考强度偏好，并在实际派发前做静态兼容性预检；任务分配交给当前主代理。

**主代理始终沿用你在 Codex 中选择的模型和思考强度。** 主代理负责规划、拆分任务与最终验收；本项目只设置执行子代理，不修改 `config.toml`，不要求主代理使用 Sol 或 Astra。

新安装默认启用模型偏好：新 Codex 会话在分派工作时会读取执行预设，无需每次输入技能名。是否分派、任务怎么拆、用哪个模型、并行多少、是否重试或继续分派，都由主代理根据任务判断；用户明确指定的分工、模型、强度和范围优先。

`0.6.2` 补充 worker 续接和恢复时的模型一致性规范：首次指定的参数不能代替后续轮次核验，无法确认保留参数的关闭 worker 应显式重建。保留项目 `tmp/` 规范、显式的项目级 `AGENTS.md` 委派规则，以及无固定并发数、重试次数、升级链和任务记录的边界。新版兼容读取旧配置，保留用户的模型与思考强度，不会为通过预检或项目初始化而自动换模型或 fallback。

## 环境要求

- Linux、macOS、WSL，或原生 Windows（PowerShell 5.1 / PowerShell 7 / CMD）。
- Python 3.10 或更高版本、Git；Linux/macOS 在线安装还需要 curl。Windows 支持 `py -3` 或 `python`。
- 已安装并登录 Codex。执行时需要当前 Codex 环境支持显式指定子代理模型和思考强度。
- 不需要另外配置 OpenAI API Key，也没有 Python 第三方依赖。

## 一键安装

Linux、macOS、WSL：

```bash
curl -fsSL https://raw.githubusercontent.com/haobanz/codex-delegate-workers/main/install.sh | bash
```

原生 Windows，在 PowerShell 中运行：

```powershell
irm https://raw.githubusercontent.com/haobanz/codex-delegate-workers/main/install.ps1 | iex
```

Windows 不需要管理员权限。安装器会添加用户级 PATH，PowerShell 安装脚本也会更新当前窗口的 PATH。安装后可直接运行 `dw`；CMD 用户安装后重新打开命令行窗口即可。

安装完成后，直接输入两个字母打开设置菜单：

```bash
dw
```

也可以通过在线命令直接打开安装和更新菜单：

```bash
curl -fsSL https://raw.githubusercontent.com/haobanz/codex-delegate-workers/main/install.sh | bash -s -- menu
```

首次安装使用默认执行配置。菜单读取终端输入，可以通过上述管道命令正常交互；没有终端的脚本环境请使用后面的非交互命令。

`delegate-workers` 和 `dw menu` 也可以打开同一个菜单。旧版用户重新运行上面的一键安装命令即可补装短命令，执行模型设置会保留。

已安装 `0.1.x` 的用户，可重新运行一键安装命令迁移到默认委派模式；也可先执行 `dw update`，再运行 `dw mode auto`。已有明确的关闭设置会在后续更新中保留。

使用旧版 `dw update` 升级时，JSON 文件可能暂时保留旧格式，但旧调度限制已不再生效。通过在线安装命令升级，或在新版中保存预设、再次更新，会生成精简配置并备份原文件。

## 设置步骤

1. 在 Codex 界面选择你希望使用的主代理模型和思考强度，本项目不会覆盖它。
2. 执行一键安装命令，再打开菜单。
3. 选择 **2. 设置执行模型和思考强度**，常用模型预设为 `default`，输入执行模型 ID，再按数字选择中文标注的思考强度。直接回车可保留当前值。
4. 再次选择 **2** 可以设置备选预设 `complex`，也可以输入新的英文预设名称。提示“设为默认执行预设？”时输入“是”或“否”。
5. 选择 **3. 查看状态和当前设置** 查看模型、版本和本地代码修改状态。
6. 选择 **4. 开启 / 关闭默认委派** 检查开关。安装后默认开启。
7. 重新启动 Codex 会话，直接描述需求即可。命令行版需要退出并重新启动 `codex`；旧会话不会自动重读启动指令。

## 默认委派模式

```bash
dw mode auto        # 开启：新会话默认使用分工规则
dw mode on-demand   # 关闭：恢复按需匹配，也可手动指定技能
dw status          # 查看开关和规则是否正常
```

开启时，安装器只在 Codex 的全局指令文件中维护一个带标记的独立段落，让主代理在分派工作时读取模型预设；具体分工仍由模型自行安排。全局模式使用全局有效的 `AGENTS.md`；如果已有非空的 `AGENTS.override.md`，则使用后者。它不会注册或修改任何项目。

原有指令按字节保留，修改前会备份。关闭或卸载时只移除本项目的指令段。若该段被手动修改、丢失，或被新建的 override 文件遮蔽，状态页会报告问题；重新执行 `dw mode auto` 可补回完整缺失的规则或迁移被遮蔽的规则，手动改过的指令段不会被直接覆盖。

这是 Codex 启动指令机制，不是后台拦截器或强制执行引擎。项目指令和用户明确要求仍然有效；客户端不支持模型指定或子代理时，会说明原因。规则检查表示文件已准备好，不能证明每个会话已加载或每次任务都已委派。规则机制参考 [Codex AGENTS.md 文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

默认配置：

| 模型预设 | 模型 | 思考强度 |
| --- | --- | --- |
| 常用模型 `default` | `gpt-5.6-luna` | 中 `medium` |
| 备选模型 `complex` | `gpt-5.6-terra` | 高 `high` |

预设表示模型偏好，不是只能使用这些模型的白名单；仓库默认仍为 `medium`。实际模型可用性、思考档位和运行容量以当前 Codex 环境为准。

默认委派开启后的使用示例（无需技能前缀）：

```text
实现这个功能。规划和验收由当前主代理负责，明确的实现任务交给执行子代理。
```

临时覆盖执行参数：

```text
$delegate-workers
这次执行优先使用 gpt-5.6-luna、high 思考强度，由你安排分工。
保留当前主代理设置，不把这次参数保存为默认值。
```

上例只是用户明确提出固定参数时的可选示例，不是所有用户或所有任务的长期要求。

## Worker 续接与恢复

模型和思考强度约定覆盖后续执行轮次。仍存活的 worker 可以正常续接；已关闭的 worker 若无法通过恢复接口明确保留/设置参数并取得新的证据，就用相同目标参数显式新建，再交接已有改动和剩余工作。同一 agent ID、`Sent input` 成功或消息里的“Luna/max”，都不是当前运行参数的证明。

宿主提供记录时，核对首次和后续轮次的模型/强度；发现不匹配就停止该 worker，确认停止后再交接，已有改动保留供审查。报告分别说明请求参数、记录参数和主代理接手的部分。没有运行元数据时如实标为未验证。这是指令层面的恢复与核验规范，不是对 Codex 恢复实现的修复或模型硬锁；完整操作边界见[配置参考](docs/configuration.md#续接恢复与运行记录)。

升级后全局默认规则随更新同步，新会话加载；已初始化的项目在各自目录运行 `dw project sync` 同步项目规则。

## 项目级 `AGENTS.md` 委派规则（显式选择加入）

项目级规则与全局默认委派相互独立。安装完成后，在 Git 项目目录或其子目录中运行这一条命令：

```bash
dw project init
```

不需要填写 `--path`、`--model` 或 `--effort`，也不需要其它设置。工具会自动定位 Git 仓库 / worktree 根；新项目继承已安装配置的默认执行预设。已有项目不带参数重复 `init` 会保留该项目已有的执行快照。

工具会创建或合并项目根的 `AGENTS.md`，保留原有内容；若已有非空 `AGENTS.override.md`，则在该活动文件中维护规则。只有目标目录不是 Git 项目时，才需要显式路径；请在该目标非 Git 目录中执行：

```bash
dw project init --path .
```

日常管理：

```bash
dw project status   # 查看状态（只读）
dw project sync    # 更新规则，保留项目配置
dw project disable # 停用项目规则
```

非 Git 目录中的上述命令也需加 `--path .`。

全局 `dw mode auto` / `dw mode on-demand` 与项目规则独立，`dw uninstall` 也不会代替项目生命周期管理；规则文件的静态检查通过，也不保证当前活动会话已经加载规则或实际运行了 worker。高级参数、快照、所有权、锁、备份、用户编辑保护和状态边界见 [项目配置文档](docs/configuration.md#项目级规则与全局模式)。

## 能力预检与 Codex 配置片段

配置工具保持三个不同的用途：

- `validate_config` 只做配置结构检查和 v1 → v2 迁移，不改变用户参数。
- `show` 返回纯结构配置，不做能力预检；因此结构合法但不兼容的旧配置仍可读取并恢复。
- `validate` 检查所有 profile 的实际模型/强度组合并逐 profile 返回 `compatibility`；`resolve` 只检查最终选中的组合（包括临时覆盖），不写回配置。

```bash
python3 skills/delegate-workers/scripts/workers.py show
python3 skills/delegate-workers/scripts/workers.py validate
python3 skills/delegate-workers/scripts/workers.py resolve --profile complex
python3 skills/delegate-workers/scripts/workers.py resolve --model gpt-5.6-luna --effort max
```

在 `resolve` 输出中，选中组合的状态路径是 `compatibility.status`；`validate` 输出则按 profile 放在 `compatibility.<profile>.status`。这两种 `workers.py` 预检结果的状态为 `compatible` 或 `unverified`，并带有来源 `source`；`runtime_verified` 始终为 `false`，因为这里没有运行时身份探测。`dw status` 是更宽的管理诊断，除这两种状态外还可能报告 `incompatible` 或 `error`。已知模型与不支持的思考强度组合会报错；未知模型保持原样并标记为 `unverified`、给出警告，不会自动换模型、profile 或 fallback。

内置快照来自 2026-09-09 本机 Codex native spawn 工具声明，仅包含：`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra` 的 `low/medium/high/xhigh/max/ultra`，`gpt-5.6-luna` 的 `low/medium/high/xhigh/max`，以及 `gpt-5.5` 的 `low/medium/high/xhigh`。它不是官方模型目录，也不证明当前账户可用。

调用者可以通过全局参数 `--capabilities PATH` 提供完整能力声明；它必须放在子命令前：

```bash
python3 skills/delegate-workers/scripts/workers.py \
  --capabilities /path/to/capabilities.json validate
```

声明格式固定为：

```json
{
  "version": 1,
  "source": "caller-provided snapshot 2026-09-09",
  "models": {
    "exact-model-id": ["low", "max"]
  }
}
```

`source` 必须是非空描述，`models` 必须非空；显式声明是调用者提供的完整集合，可以覆盖内置快照，但其中没有的模型会被拒绝，不会回落内置数据。不要编造 catalog 只为通过检查。`dw configure` 按内置快照检查新设置；`dw status` 诊断每个 profile，人类菜单也展示诊断。已有不兼容配置仍可查看，再修正强度后保存。

只读命令 `codex-config` 通过预检后仅输出下面两个 `[agents]` 键的 TOML 片段，例如：

```bash
python3 skills/delegate-workers/scripts/workers.py \
  codex-config --model gpt-5.6-luna --effort max
```

```toml
[agents]
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "max"
```

它不读取或写入全局 `config.toml`，不自动安装，也不修改主模型。若配置中已有 `[agents]` 表，应审核后把两个键合并进去，不要重复声明表。未知模型的警告写入 stderr，不污染 stdout 的 TOML 片段。两个字段是可选的原生默认值，不是硬锁；显式 spawn 值优先，且不能保证所有客户端版本都支持。字段含义和优先级以 [OpenAI 官方 Subagents 全局设置](https://learn.chatgpt.com/docs/agent-configuration/subagents#global-settings) 为准。

## 一键更新

已安装时：

```bash
dw update
```

或者使用在线更新命令：

```bash
curl -fsSL https://raw.githubusercontent.com/haobanz/codex-delegate-workers/main/install.sh | bash -s -- update
```

菜单中的 **1. 安装 / 更新** 也会执行安装或更新。更新菜单完成后会退出，重新打开即可加载新版管理工具；重新启动 Codex 会话可加载更新后的 Skill 和默认委派规则。

- 更新保留你的执行模型和思考强度；迁移旧格式时移除已经停用的调度限制，原文件留有备份。
- 更新保留默认委派开关；默认委派处于开启状态时，也会更新本工具维护的规则段。
- 更新前备份旧版本，检查必要文件、模型配置、Python 脚本和管理入口能否启动，再替换安装目录。
- 能力预检模块与内置快照作为成对运行时依赖纳入候选清单；回滚到合法旧版本时按旧版本清单处理，不会被当前版本的能力文件要求误拒。
- 网络失败、配置不兼容或替换失败时，保留或恢复旧版本。
- 如果你修改了安装目录里的 Skill 代码，更新会报告冲突，不覆盖这些修改。
- **5. 回滚版本（保留执行设置）** 回滚代码并保留当前执行设置；如果旧代码无法读取当前设置，会停止回滚。

## 非交互设置

更改默认执行模型与强度（各系统通用）：

```bash
dw configure --profile default --model gpt-5.6-luna --effort high
```

设置一个备选模型预设：

```bash
dw configure --profile complex --model gpt-5.6-terra --effort xhigh
```

将已有预设设为默认：

```bash
dw configure --profile complex --default
```

状态、回滚、卸载：

```bash
dw status
dw rollback
dw uninstall --yes
```

菜单和终端中的状态输出为中文；重定向或管道中的结果仍保留原有 JSON 字段，方便脚本读取。

卸载移除本项目的 Skill、启动器及默认委派规则段，并保留可恢复的备份。不会删除其他 Skill、个人指令、项目文件或 Codex 主代理配置。

Windows 卸载还会移除本安装器添加的用户 PATH 条目；原本就存在的条目不会删除。

## 安装位置

技能默认使用 `~/.codex`，Windows 对应 `%USERPROFILE%\.codex`；如果设置了 `CODEX_HOME`，使用该路径。以下是 Linux/macOS 的目录：

```text
~/.local/bin/
  dw                                  短命令，默认打开菜单
  delegate-workers                    完整命令
~/.codex/
  AGENTS.md                            默认委派规则段（有 override 时使用 override）
  bin/delegate-workers                 兼容旧版本的命令入口
  skills/delegate-workers/             Skill 与管理工具
    workers.json                       唯一的持久执行模型配置
    workers.json.bak                   最近一次设置修改前的备份
    model-capabilities.json             带来源的内置能力快照
    scripts/capabilities.py             能力声明校验模块
    .delegate-workers-install.json     安装版本与受管理文件记录
  delegate-workers-backups/            代码和指令文件备份
```

短命令默认安装到 `~/.local/bin`。安装器检查 PATH，如果当前 Shell 没有包含该目录，会输出一次性的设置命令：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

将这行加入 Bash 的 `~/.bashrc` 或 Zsh 的 `~/.zshrc` 可使新终端也生效。安装器不修改 Shell 启动文件或 `config.toml`，也不会覆盖已有的同名程序；开启默认委派时只在全局指令文件中维护本项目的标记段落。

通过 `--codex-home` 指定隔离安装目录时，命令默认放在该目录的 `bin` 中；可以用 `--bin-dir PATH` 明确指定短命令位置。

Windows 短命令默认位于 `%LOCALAPPDATA%\Programs\DelegateWorkers\bin`，包含 `dw.cmd`、`delegate-workers.cmd` 和 Python 入口文件。中文界面使用 UTF-8，安装路径可以包含空格和中文。隔离安装不会修改用户 PATH。

## 本地开发与测试

临时文件统一放在项目根目录的 `tmp/`，不存在时自动创建；安装或更新从 Git 子目录运行时使用最近的仓库 / worktree 根，没有 Git 根时使用运行命令的当前目录。请先进入项目再运行安装或更新。各次下载、测试使用独立子目录并清理自身内容，目录不可用时直接报错，不回退到系统临时目录。本仓库已忽略 `tmp/`；在其他项目也不要提交临时产物。

这条规范同时写入 Skill、全局默认规则和项目规则模板。升级会更新已开启的全局默认规则；已初始化的项目需在各自目录运行 `dw project sync` 更新规则，然后重开 Codex 会话。工具内部用于原子替换的短暂文件和安装事务暂存仍紧邻目标文件，并在事务结束时清理，避免跨磁盘替换失败；它们不使用系统临时目录。

从源码安装：

```bash
python3 skills/delegate-workers/scripts/manage.py --source . install
```

Windows 从源码安装：

```powershell
.\install.ps1 -Source .
```

Windows 隔离安装及测试：

```powershell
.\install.ps1 -Source . -CodexHome "$PWD\tmp\delegate-workers-demo" -NoPath
py -3 -X utf8 -m unittest discover -s tests -v
.\tests\windows_bootstrap.ps1
```

隔离安装测试：

```bash
python3 skills/delegate-workers/scripts/manage.py \
  --codex-home ./tmp/delegate-workers-demo --source . install
./tmp/delegate-workers-demo/bin/dw
```

运行验证：

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
python3 skills/delegate-workers/scripts/workers.py validate
```

模型预设与预检工具用法见 [配置参考](docs/configuration.md)。工具只读取、校验和保存执行参数，不接管主代理的规划、调度或主模型。

仓库提供 Linux、macOS、Windows 的 Python 3.10 / 3.13 及 Windows PowerShell/CMD 测试路径；各平台是否通过以实际 CI 运行结果为准，本文不替代测试报告。

本项目为独立实现，现有社区项目仅作为设计参考，没有安装或引入其代码。Codex 原生能力参考：[Skills](https://learn.chatgpt.com/docs/build-skills)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。
