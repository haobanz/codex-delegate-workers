## Project Worker Delegation

For this project, delegate concrete execution tasks to a worker using exactly
this requested pair:

- model: `{model}`
- reasoning effort: `{reasoning_effort}`

The main agent keeps its current model and reasoning effort unchanged. The main
agent owns planning, architecture, review, and final acceptance; the execution
worker works only within the user's approved boundary and reports:

- the actual dispatch or agent ID when the host provides one;
- the explicit model and reasoning-effort parameters requested;
- changed files;
- tests and concrete evidence; and
- unverified items.

The requested pair is a request, not proof of runtime identity. Never claim
that the requested model or effort was independently verified unless the host
reports it. If either choice is unavailable, stop and ask the user for an
alternative; never silently switch. Honor the current user's instructions and
higher-priority instructions.

Read the installed `delegate-workers` skill and its current configuration when
the task requires its guidance. Do not install, update, activate, or change
global settings while handling an ordinary project task.

### 续接与恢复时的模型一致性

本项目指定的模型和思考强度适用于执行 worker 的后续轮次。完成一轮不等于
已关闭；仍存活且配置保持的 worker 可正常续接。若 `resume_agent` 或其他
隐式恢复路径不能明确保留/设置指定参数并提供新的证据，不直接恢复已关闭
的 worker；显式创建新 worker 并传入上面的模型/强度，交接已有改动和剩余
任务。同一 ID、`Sent input` 成功和消息中的模型名均不能设置或核验模型。

宿主提供元数据时，核对首次及每次续接的新轮次记录，关联实际 agent ID 和
轮次/时间。恢复/重建后如需先启动才能取得记录，先给仅检查、不改文件的
交接，再交实现任务；元数据缺失或过期标为未验证。用户要求先核验后执行而
宿主无法提供时，说明限制并询问替代方案。

发现不匹配时停止派活并中断或关闭旧 worker，确认停止后再把同一批文件交
给显式创建的替代 worker。无法确认停止时，说明限制，暂停重叠写入，可继续
独立工作。保留改动供审查；不用文字消息尝试切换模型，不修改主模型或全局
默认值。分别报告请求参数、观察到的参数及受影响轮次，主
代理接手单独说明；后续匹配不能抹去先前不匹配。运行记录只证明记录的配置，
不证明供应商上游的物理模型身份。无需固定重试、并发上限或任务记录文件。

### 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在本项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。
