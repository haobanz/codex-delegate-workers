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

### 已启用的统一接口

原生派发能接受当前选择时继续用它。原生派发无法接受该组合、而当前会话里能
看到本工具自己的 `delegate-workers` MCP 工具时，优先使用它，不要临时拼一条
等价的 CLI 命令：先用 `list_models`（传项目 `cwd`）读取已配置偏好和当前生效
来源，再用 `spawn_agent` 按当前用户或项目选择传显式模型和强度，用 `get_agent`
轮询状态，需要停止时用 `cancel_agent`。`list_models` 只反映已配置偏好，不是
供应商完整模型目录，其中没有某模型不代表账户不可用。

调用者明确给定模型和强度时要显式传入：给出 `model` 就必须同时给出
`reasoning_effort`，也可以改用显式 `profile`。任务不修改文件时保持 `sandbox`
默认的 `read-only`；`writable_files` 是交接边界的说明，不是每文件级别的强制
ACL。每次 `spawn_agent` 都是新的独立进程，当前 MVP 没有 resume 工具；继续工作
要显式新建并做有限交接，不要把新任务当作旧会话的隐式续接。接口返回的
`agent_id` 不是后端 CLI 会话 id；只报告工具实际给出的标识，并把请求参数与
观察到的启动配置分开记录。

当前会话看不到这些工具时，不要在普通任务里安装、注册或修改全局设置；明确
报告这一限制，由用户决定是否启用。下面精确的本地 CLI 路径在既有授权范围内
仍然可用。

### 派发可用性判断与路由选择

在声称“无法派发”之前，先检查当前启用的工具 schema / 清单和可用的文档化
发现路径，并把不同情况分开：派发工具不存在；该接口拒绝了指定模型；供应商、
认证或网络错误；缺少运行时身份元数据；以及某个业务工具（例如表格或浏览器
连接）断开。任何一项都不等于另一项，过期的运行时快照也不构成对某模型的
账户级否定。

路由选择不是固定重试或 fallback 链，`list_models`、兼容性预检和候选路径
检查都是本地静态操作，不发起认证或模型请求。原生派发拒绝某个组合只说明该
宿主接口不接受它，不代表模型不可用：可以在既有授权内用原 ID 尝试已启用的
统一接口或本机 CLI，成功与否取决于本机 CLI 版本和供应商支持，不因换路径而
保证执行。已经派发出去的任务失败时，先查看该 agent 的状态和失败原因再决定
是否新建派发；同一个供应商的失败不会因为改走直连 CLI 就自动变成成功，也不
要求重试、更换 worker 或额外授权。原生派发能接受所请求组合时仍然可用。

模型和强度以当前用户或项目选择为准，之前工作里引用的示例不能覆盖它。使用
本地 CLI 路径前先核对该 CLI 的真实 help 与功能，向子进程显式传入所请求的
模型和强度，并保持主会话不变。不得静默替换模型或供应商、修改全局配置、
索取 API Key，或仅为这条路径安装组件。可先用只读、仅检查的调用确认路径
是否可用。

统一接口和独立 CLI worker 都是独立进程，不是原生 agent。只报告该路径实际
返回的 `agent_id` 或 session id；不要把借用的或父会话的 id 当作 worker 的。
会话头部记录的是所请求的配置，一次成功响应说明路径可用；两者都不证明上游
供应商的物理模型身份。

还要确认所选路径确实具备任务需要的工具和文件：CLI worker 可能没有主会话
拥有的业务工具连接，例如表格或浏览器集成。工具可见性不能靠改写指令来编造
或承诺。依赖重试或分批传输读取的任务要核实预期覆盖是否完整，不要假定成功；
这条要求保持通用，不写死记录范围或分块大小，也不声称修复上游工具。

已有明确分工或适用项目规则要求特定模型/强度、而没有任何已授权途径能执行
时，只暂停被阻塞的执行，说明具体限制并询问替代方案。主代理可以继续规划和
做定位阻塞所需的最小诊断，但不得以“只读调查”“重试”或“fallback”等名义
代做被分配的业务任务；披露限制不等于获得替代授权。既有的主代理接手明确
批准可直接使用，无需重复索取。分别报告请求与观察到的参数、以及主代理接手
的工作，本身也不构成接手授权。“可以继续独立工作”指已授权分工范围内的
工作，不包括被阻塞的任务。可选预设仍保留主代理的一般自主权：本节不引入
调度器、固定重试次数、并发上限、审计台账或新的能力文件，普通任务也不强制
委派。

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
暂停重叠写入；已授权分工范围内的独立工作可以继续。保留已有改动供审查，
不用文字消息尝试切换模型，不修改主模型或全局默认值。分别报告请求参数、
观察到的参数及受影响轮次，主代理接手的工作单独说明；后续匹配不能抹去
先前不匹配。运行记录只证明记录的配置，不证明供应商上游的物理模型身份。

### 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在当前项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。
