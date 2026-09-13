## Default Worker Delegation

For coding tasks that benefit from delegation, read the delegate-workers skill at
{skill_path} and its current
`workers.json`. Use native subagents when useful; the presets are preferences,
not a scheduler.

The user's explicit division of labor, requested worker model or effort, file
boundary, and acceptance criteria take priority. Never silently switch a
requested profile, model, effort, or fallback. If the requested choice is
unavailable, say so plainly and ask for an alternative. Without an explicit
extra constraint, the main agent keeps general autonomy over whether and how to
delegate.

Keep the main agent's own model and reasoning effort unchanged; never copy worker
settings into the main session. The main agent owns planning, architecture,
review, and final acceptance. An execution agent works directly within the
requested scope; do not recurse mechanically into more agents.

Pass only the necessary objective, writable file boundary, interface context,
and acceptance criteria, not the entire task history. When the host supports
explicit worker parameters, use its actual schema and pass the requested model
and reasoning effort explicitly. A dispatch request, a worker's completed turn,
and main-agent acceptance are separate states; report them accurately.

At handoff, report the real agent id only if the host provides one, requested
model and effort, changed files, tests, and unverified items. If the host does
not report actual model identity, do not claim independent identity
verification. If delegation is unavailable, report the concrete limitation.

Honor an explicit request not to delegate and applicable project instructions.
For ordinary work, do not install, update, or change global settings. This rule
does not impose a task record, fixed output format, concurrency cap, retry loop,
or fallback scheduler.

### 续接与恢复时的模型一致性

选定的执行模型和思考强度也适用于后续轮次，不能只检查首次创建。完成一轮
不等于已关闭；仍存活且配置保持的 worker 可正常续接。对已关闭、恢复或
连接状态不明的 worker，先确认宿主实际状态和工具语义。若 `resume_agent`
或隐式恢复路径不能明确保留/设置所选参数并提供新的证据，就显式创建新
worker，传入 `model` 和 `reasoning_effort`，交接已有改动和剩余任务。
同一 agent ID、`Sent input` 成功、在消息里写模型名，都不能设置或核验模型。

宿主提供元数据时，核对首次及每次续接的新轮次记录，关联实际 agent ID 和
轮次/时间；不要沿用首次记录。恢复/重建后如需先启动才能取得记录，先给
仅检查、不改文件的交接，再交实现任务。元数据缺失或过期标为未验证；用户
要求先核验后执行而宿主无法提供时，说明限制并询问替代方案。

发现记录与所选模型/强度不一致，停止派活并中断或关闭该 worker；确认停止
后再把同一批文件交给显式创建的替代 worker。无法确认停止时，说明限制，
暂停重叠写入，可继续独立工作。保留已有改动供审查，不用文字消息尝试切换
模型，不修改主模型或全局默认值。分别报告请求参数、观察
到的参数及受影响轮次，主代理接手的工作单独说明；后续匹配不能抹去先前
不匹配。运行记录只证明记录的配置，不证明供应商上游的物理模型身份。

### 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在当前项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。
