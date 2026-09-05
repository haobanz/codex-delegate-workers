# Delegate Workers

在 Codex 中，将边界明确的执行任务交给可配置的子代理。

**主代理始终沿用你在 Codex 中选择的模型和思考强度。** 主代理负责规划、拆分任务与最终验收；本项目只设置执行子代理，不修改 `config.toml`，不要求主代理使用 Sol 或 Astra。

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

## 设置步骤

1. 在 Codex 界面选择你希望使用的主代理模型和思考强度，本项目不会覆盖它。
2. 执行一键安装命令，再打开菜单。
3. 选择 **2. Worker model settings**，选择 `default`，输入执行模型 ID 和思考强度。直接回车可保留当前值。
4. `Fallback profile` 填 `complex`，表示允许考虑该执行角色进行失败升级；填 `none` 可关闭这个角色的升级路径。
5. 再次选择 **2** 可以设置 `complex`，也可以输入新的角色名称。`Set as the default worker` 可选择是否将该角色设为默认执行角色。
6. 选择 **3. Concurrency / attempts** 设置最多并行子代理数和单个任务最多尝试次数。
7. 选择 **4. Status / validate** 查看配置、版本和本地代码修改状态。
8. 在 Codex 中新建任务，输入 `$delegate-workers` 和具体需求。若技能列表仍未刷新，重启 Codex。

默认配置：

| 执行角色 | 模型 | 思考强度 | 后备角色 |
| --- | --- | --- | --- |
| `default` | `gpt-5.6-luna` | `medium` | `complex` |
| `complex` | `gpt-5.6-terra` | `high` | 无 |

默认最多并行 3 个执行子代理，每个任务最多尝试 2 次。实际模型可用性、思考档位和并发容量以当前 Codex 环境为准。

使用示例：

```text
$delegate-workers
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

菜单中的 **1. Install / update** 也会执行安装或更新。更新菜单完成后会退出，重新打开即可加载新版管理工具；新建 Codex 任务可加载更新后的 Skill 指令。

- 更新保留你自己的执行模型配置 `workers.json`。
- 更新前备份旧版本，先验证新代码能读取现有设置，再替换安装目录。
- 网络失败、配置不兼容或替换失败时，保留或恢复旧版本。
- 如果你修改了安装目录里的 Skill 代码，更新会报告冲突，不覆盖这些修改。
- **5. Roll back code** 回滚代码并保留当前执行设置；如果旧代码无法读取当前设置，会停止回滚。

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

卸载仅移除本项目的 Skill 和启动器，并保留可恢复的备份。不会删除其他 Skill、项目文件或 Codex 主代理配置。

## 安装位置

默认使用 `~/.codex`；如果设置了 `CODEX_HOME`，使用该路径。

```text
~/.local/bin/
  dw                                  短命令，默认打开菜单
  delegate-workers                    完整命令
~/.codex/
  bin/delegate-workers                 兼容旧版本的命令入口
  skills/delegate-workers/             Skill 与管理工具
    workers.json                       唯一的持久执行模型配置
    workers.json.bak                   最近一次设置修改前的备份
    .delegate-workers-install.json     安装版本与受管理文件记录
  delegate-workers-backups/            更新与卸载备份
```

短命令默认安装到 `~/.local/bin`。安装器检查 PATH，如果当前 Shell 没有包含该目录，会输出一次性的设置命令：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

将这行加入 Bash 的 `~/.bashrc` 或 Zsh 的 `~/.zshrc` 可使新终端也生效。安装器不修改 Shell 启动文件、`AGENTS.md` 或 `config.toml`，也不会覆盖已有的同名程序。

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
