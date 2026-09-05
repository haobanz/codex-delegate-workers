# Delegate Workers

在 Codex 中设置执行子代理的模型和思考强度，任务分配交给当前主代理。

**主代理始终沿用你在 Codex 中选择的模型和思考强度。** 主代理负责规划、拆分任务与最终验收；本项目只设置执行子代理，不修改 `config.toml`，不要求主代理使用 Sol 或 Astra。

新安装默认启用模型偏好：新 Codex 会话在分派工作时会读取执行预设，无需每次输入技能名。是否分派、任务怎么拆、用哪个模型、并行多少、是否重试或继续分派，都由模型根据任务判断。

`0.3.0` 已移除固定并发数、重试次数、升级链和子代理继续分派限制，也不要求维护任务记录。新版兼容读取旧配置，并保留原有模型与思考强度。

## 环境要求

- Linux、macOS 或 WSL。
- Python 3.10 或更高版本、Git；在线一键安装还需要 curl。
- 已安装并登录 Codex。执行时需要当前 Codex 环境支持显式指定子代理模型和思考强度。
- 不需要另外配置 OpenAI API Key，也没有 Python 第三方依赖。

## 一键安装

公开仓库可直接运行：

```bash
curl -fsSL https://raw.githubusercontent.com/haobanz/codex-delegate-workers/main/install.sh | bash
```

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

开启时，安装器在 Codex 的全局指令文件中维护一个带标记的独立段落，让主代理在分派工作时读取模型预设。具体分工由模型自行安排。它使用当前有效的 `AGENTS.md`；如果已有非空的 `AGENTS.override.md`，则使用后者。

原有指令按字节保留，修改前会备份。关闭或卸载时只移除本项目的指令段。若该段被手动修改、丢失，或被新建的 override 文件遮蔽，状态页会报告问题；重新执行 `dw mode auto` 可补回完整缺失的规则或迁移被遮蔽的规则，手动改过的指令段不会被直接覆盖。

这是 Codex 启动指令机制，不是后台拦截器或强制执行引擎。项目指令和用户明确要求仍然有效；客户端不支持模型指定或子代理时，会说明原因。规则检查表示文件已准备好，不能证明每个会话已加载或每次任务都已委派。规则机制参考 [Codex AGENTS.md 文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

默认配置：

| 模型预设 | 模型 | 思考强度 |
| --- | --- | --- |
| 常用模型 `default` | `gpt-5.6-luna` | 中 `medium` |
| 备选模型 `complex` | `gpt-5.6-terra` | 高 `high` |

预设表示模型偏好，不是只能使用这些模型的白名单。实际模型可用性、思考档位和运行容量以当前 Codex 环境为准。

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
- 网络失败、配置不兼容或替换失败时，保留或恢复旧版本。
- 如果你修改了安装目录里的 Skill 代码，更新会报告冲突，不覆盖这些修改。
- **5. 回滚版本（保留执行设置）** 回滚代码并保留当前执行设置；如果旧代码无法读取当前设置，会停止回滚。

## 非交互设置

更改默认执行模型与强度：

```bash
dw configure \
  --profile default --model gpt-5.6-luna --effort high
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

## 安装位置

默认使用 `~/.codex`；如果设置了 `CODEX_HOME`，使用该路径。

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
    .delegate-workers-install.json     安装版本与受管理文件记录
  delegate-workers-backups/            代码和指令文件备份
```

短命令默认安装到 `~/.local/bin`。安装器检查 PATH，如果当前 Shell 没有包含该目录，会输出一次性的设置命令：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

将这行加入 Bash 的 `~/.bashrc` 或 Zsh 的 `~/.zshrc` 可使新终端也生效。安装器不修改 Shell 启动文件或 `config.toml`，也不会覆盖已有的同名程序；开启默认委派时只在全局指令文件中维护本项目的标记段落。

通过 `--codex-home` 指定隔离安装目录时，命令默认放在该目录的 `bin` 中；可以用 `--bin-dir PATH` 明确指定短命令位置。

## 本地开发与测试

从源码安装：

```bash
python3 skills/delegate-workers/scripts/manage.py --source . install
```

隔离安装测试：

```bash
python3 skills/delegate-workers/scripts/manage.py \
  --codex-home /tmp/delegate-workers-demo --source . install
/tmp/delegate-workers-demo/bin/dw
```

运行验证：

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
python3 skills/delegate-workers/scripts/workers.py validate
```

模型预设工具用法见 [配置参考](docs/configuration.md)。工具只读取和保存模型参数，不接管主代理的规划和调度。

本项目为独立实现，现有社区项目仅作为设计参考，没有安装或引入其代码。Codex 原生能力参考：[Skills](https://learn.chatgpt.com/docs/build-skills)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。
