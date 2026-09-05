# Delegate Workers

在 Codex 中，将边界明确的执行任务交给可配置的子代理。

**主代理始终沿用你在 Codex 中选择的模型和思考强度。** 主代理负责规划、拆分任务与最终验收；本项目只设置执行子代理，不修改 `config.toml`，不要求主代理使用 Sol 或 Astra。

从 `0.2.0` 开始，新安装默认开启委派规则：新 Codex 会话开始后，较大的编程任务会按规则使用执行子代理，不需要每次输入技能名。问答和简单修改仍可直接完成。

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

## 设置步骤

1. 在 Codex 界面选择你希望使用的主代理模型和思考强度，本项目不会覆盖它。
2. 执行一键安装命令，再打开菜单。
3. 选择 **2. 设置执行模型和思考强度**，常规执行角色为 `default`，输入执行模型 ID，再按数字选择中文标注的思考强度。直接回车可保留当前值。
4. “后备角色名称”填 `complex`，表示允许考虑该执行角色进行失败升级；填“无”可关闭这个角色的升级路径。
5. 再次选择 **2** 可以设置困难任务角色 `complex`，也可以输入新的英文角色名称。提示“设为默认执行角色？”时输入“是”或“否”。
6. 选择 **3. 设置并发数和尝试次数** 设置最多并行子代理数和单个任务最多尝试次数。
7. 选择 **4. 查看状态和当前设置** 查看中文配置摘要、版本和本地代码修改状态。
8. 选择 **7. 开启 / 关闭默认委派** 检查开关。安装后默认开启；状态页会显示启动规则的位置及是否完整。
9. 重新启动 Codex 会话，直接描述需求即可。命令行版需要退出并重新启动 `codex`；旧会话不会自动重读启动指令。

## 默认委派模式

```bash
dw mode auto        # 开启：新会话默认使用分工规则
dw mode on-demand   # 关闭：恢复按需匹配，也可手动指定技能
dw status          # 查看开关和规则是否正常
```

开启时，安装器在 Codex 的全局指令文件中维护一个带标记的独立段落，明确要求先读取本技能，再将边界清楚的实现任务交给配置好的执行模型。它使用当前有效的 `AGENTS.md`；如果已有非空的 `AGENTS.override.md`，则使用后者。

原有指令按字节保留，修改前会备份。关闭或卸载时只移除本项目的指令段。若该段被手动修改、丢失，或被新建的 override 文件遮蔽，状态页会报告问题；重新执行 `dw mode auto` 可补回完整缺失的规则或迁移被遮蔽的规则，手动改过的指令段不会被直接覆盖。

这是 Codex 启动指令机制，不是后台拦截器或强制执行引擎。项目指令和用户明确要求仍然有效；客户端不支持模型指定或子代理时，会说明原因。规则检查表示文件已准备好，不能证明每个会话已加载或每次任务都已委派。规则机制参考 [Codex AGENTS.md 文档](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

默认配置：

| 执行角色 | 模型 | 思考强度 | 后备角色 |
| --- | --- | --- | --- |
| 常规执行 `default` | `gpt-5.6-luna` | 中 `medium` | `complex` |
| 困难任务 `complex` | `gpt-5.6-terra` | 高 `high` | 无 |

默认最多并行 3 个执行子代理，每个任务最多尝试 2 次。实际模型可用性、思考档位和并发容量以当前 Codex 环境为准。

默认委派开启后的使用示例（无需技能前缀）：

```text
实现这个功能。规划和验收由当前主代理负责，明确的实现任务交给执行子代理。
```

临时覆盖执行参数：

```text
$delegate-workers
这次执行使用 gpt-5.6-luna、high 思考强度，最多并行 2 个子代理。
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

- 更新保留你自己的执行模型配置 `workers.json`。
- 更新保留默认委派开关；默认委派处于开启状态时，也会更新本工具维护的规则段。
- 更新前备份旧版本，先验证新代码能读取现有设置，再替换安装目录。
- 网络失败、配置不兼容或替换失败时，保留或恢复旧版本。
- 如果你修改了安装目录里的 Skill 代码，更新会报告冲突，不覆盖这些修改。
- **5. 回滚版本（保留执行设置）** 回滚代码并保留当前执行设置；如果旧代码无法读取当前设置，会停止回滚。

## 非交互设置

更改默认执行模型与强度：

```bash
dw configure \
  --profile default --model gpt-5.6-luna --effort high
```

关闭默认角色的失败升级：

```bash
dw configure \
  --profile default --fallback none
```

设置并发和尝试次数：

```bash
dw limits --parallel 2 --attempts 3
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

执行参数工具用法见 [配置参考](docs/configuration.md)。计数由主代理维护，工具用于检查派发条件，不是后台调度器，也不提供金额预算的硬限制。

本项目为独立实现，现有社区项目仅作为设计参考，没有安装或引入其代码。Codex 原生能力参考：[Skills](https://learn.chatgpt.com/docs/build-skills)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。
