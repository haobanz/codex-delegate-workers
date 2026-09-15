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

### 已启用的统一接口

原生派发能接受 `{model}` / `{reasoning_effort}` 时继续用它。原生派发无法接受
该组合、而当前会话里能看到本工具自己的 `delegate-workers` MCP 工具时，优先
使用它：先用 `list_models`（传项目 `cwd`）确认本项目快照和当前生效来源，再用
`spawn_agent` 显式传入 `{model}` 和 `{reasoning_effort}`，用 `get_agent` 轮询
状态，需要停止时用 `cancel_agent`。`list_models` 只反映已配置偏好，不是供应商
完整模型目录；其中没有某模型不代表账户不可用。给出 `model` 时必须同时给出
`reasoning_effort`。任务不修改文件时保持 `sandbox` 默认的 `read-only`；
`writable_files` 是交接边界说明，不是每文件级别的强制 ACL。每次 `spawn_agent`
都是新的独立进程，当前 MVP 没有 resume 工具，继续工作要显式新建并做有限
交接。接口返回的 `agent_id` 不是后端 CLI 会话 id；只报告工具实际给出的标识，
并把请求参数与观察到的启动配置分开记录。当前会话看不到这些工具时，不在普通
任务里安装、注册或修改全局设置，而是报告限制并让用户决定；下面的本地 CLI
路径在既有授权范围内仍然可用。

### 派发可用性判断与路由选择

声称“无法派发”之前，先检查当前启用的工具 schema / 清单和可用的文档化发现
路径，并区分：工具不存在；接口拒绝 `{model}`；供应商、认证或网络错误；缺少
运行时身份元数据；以及业务工具（如表格或浏览器连接）断开。各项互不推断，
过期的运行时快照也不构成账户级否定。模型和强度以本项目规则及当前用户选择
为准，之前工作里引用的示例不能覆盖。

路由选择不是固定重试或 fallback 链，`list_models`、兼容性预检和路径检查都是
本地静态操作，不发起认证或模型请求。原生派发拒绝 `{model}` 只说明该宿主接口
不接受它，不代表模型不可用：可以在既有授权内用原 ID 尝试统一接口或本机 CLI，
成功与否取决于本机 CLI 版本和供应商支持，不因换路径而保证执行。已经派发出去
的任务失败时，先查看该 agent 的状态和失败原因再决定是否新建派发；同一个供应
商的失败不会因为改走直连 CLI 就自动变成成功，也不要求重试、更换 worker 或额
外授权。原生派发能接受该组合时仍然可用。

使用本机 CLI 路径前先核对 CLI 真实 help 与功能，向子进程显式传入 `{model}`
和 `{reasoning_effort}`，并保持主会话不变；不得静默替换模型或供应商、修改全
局配置、索取 API Key，或仅为这条路径安装组件。统一接口和独立 CLI worker 都是
独立进程而非原生 agent；只报告该路径实际返回的 `agent_id` 或 session id，
不要把借用的或父会话的 id 当作 worker 的。会话头部和成功响应只说明记录的配置
与路径可用，都不证明上游物理模型身份。还要确认该路径具备任务所需工具和文件：
CLI worker 可能没有主会话的业务工具连接，工具可见性不能靠改写指令编造；重试
或分批读取要核实预期覆盖完整，且不写死范围或分块大小。

没有任何已授权途径能以 `{model}` / `{reasoning_effort}` 执行时，只暂停被阻塞
的执行，说明具体限制并询问替代方案。主代理可继续规划和做定位阻塞所需的最小
诊断，但不得以“只读调查”“重试”或“fallback”等名义代做被分配的业务任务；
披露限制不等于授权接手，分别报告请求与观察到的参数也不构成接手授权。既有的
主代理接手明确批准可直接使用。本节不引入调度器、固定重试、并发上限、审计
台账或新的能力文件；全局默认预设仍保留主代理的一般自主权。

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
给显式创建的替代 worker。无法确认停止时，说明限制，暂停重叠写入；已授权
分工范围内的独立工作可以继续。保留改动供审查；不用文字消息尝试切换模型，
不修改主模型或全局默认值。分别报告请求参数、观察到的参数及受影响轮次，
主代理接手单独说明；后续匹配不能抹去先前不匹配。运行记录只证明记录的
配置，不证明供应商上游的物理模型身份。无需固定重试、并发上限或任务记录
文件。

### 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在本项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。
