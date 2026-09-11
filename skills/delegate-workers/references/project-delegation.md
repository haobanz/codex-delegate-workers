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

### 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在本项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。
