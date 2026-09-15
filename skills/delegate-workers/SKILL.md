---
name: delegate-workers
description: Use configured model and reasoning presets with compatibility preflight when delegating execution work in Codex. The main agent retains planning and acceptance.
---

# Delegate Workers

Use this skill as a lightweight execution-preset and compatibility-preflight aid
when the main Codex agent considers subagents. It is not an autonomous scheduler,
and it does not force delegation.

Keep the main agent's own model and reasoning effort unchanged; never copy worker
settings into the main session.

## 临时文件规范

主代理和所有子代理的临时文件、草稿、日志、截图、下载和测试数据，统一
放在当前项目根目录的 `tmp/`；不存在时先创建，从子目录工作时也使用同一
位置。使用能识别用途的独立子目录，并在委派时传递其绝对路径。临时命令
显式指定目录，必要时仅为该命令设置 `TMPDIR`、`TMP`、`TEMP`；不要使用
系统 `/tmp`、`/var/tmp` 或系统 `%TEMP%`。无法创建或写入时报告问题，不
回退到系统临时目录。不要提交临时产物，只清理本次创建的内容。

## Presets and preflight

Read [workers.json](workers.json), or run
`<python> <skill-path>/scripts/workers.py show`, where `<skill-path>` is this
skill's directory. Use `python3` on Linux/macOS, or `py -3` (or `python`) on
Windows. A user-supplied config is selected with `--config PATH` before the
subcommand.

`validate_config` performs structural validation and v1-to-v2 migration only.
`show` returns that structural configuration without capability checking, so a
legacy or otherwise incompatible configuration remains readable for recovery.
`validate` checks every profile's actual model/effort pair. `resolve` checks only
the selected final pair after any temporary override and never writes the
configuration. `resolve` exposes the selected result at
`compatibility.status`; `validate` returns one result per profile at
`compatibility.<profile>.status`. Those `workers.py` results use `compatible` or
`unverified`, with `runtime_verified` always `false` because this is a static
preflight, not a runtime identity check. `dw status` is a broader management
diagnostic and may also report `incompatible` or `error`.

The bundled `model-capabilities.json` is a dated 2026-09-09 local native-spawn
schema snapshot, not an official model catalog and not proof that an account can
use a model. A known model with an unsupported effort is rejected. An unknown
model stays unchanged and is reported as `unverified`; never silently replace it,
choose another profile, or invent a fallback. `--capabilities PATH` is a global
option and must appear before the subcommand. Its JSON must be a complete,
strictly validated caller declaration; it can override the bundled snapshot, but
a model absent from it is rejected. Do not fabricate a catalog just to pass the
preflight. If the real host has not provided the model, confirm with the host or
ask the user for an alternative arrangement; do not change capability data to
make the request fit.

`codex-config` is a read-only helper that emits only the two `[agents]` defaults
(`default_subagent_model` and `default_subagent_reasoning_effort`) as a TOML
fragment. It does not read or write `config.toml`, install anything, or change
the main agent. These are optional native defaults, not a hard lock; explicit
spawn values take precedence, and client support can vary. If an `[agents]`
table already exists, merge the two keys into it rather than adding a duplicate
table. Use the skill-local [model-capabilities.json](model-capabilities.json) as
the schema example and `workers.py --help` for CLI syntax. Any unknown-model
warning goes to stderr, not into the TOML fragment on stdout.

## Project-scoped AGENTS.md rules

When a user has explicitly opted a project into the project rule, read the
skill-local [project delegation template/context](references/project-delegation.md)
to understand the generated managed rule; do not assume it documents commands
unless it says so. Keep this opt-in project-scoped: use
`dw project status` as a read-only diagnostic and suggest or run `dw project
sync` only when an authorized explicit update is relevant. Do not scan every
task, continuously regenerate files, implicitly opt in, or modify unrelated
projects. `dw project disable` is separate from global `dw mode` and
`dw uninstall`.

The project state file `.delegate-workers-project.json` carries the worker
snapshot and ownership metadata; keep it with the generated `AGENTS.md` when
sharing that project lifecycle. The root `.delegate-workers-project.lock` is a
stable persistent operation lock, and
`.delegate-workers-project-backups/<timestamp-id>/` contains a manifest and
preimages for recovery. Treat the lock and backup directory as local artifacts
to ignore according to the repository's policy; do not modify `.gitignore`
automatically. The generated reference is template/context only, not a command
or lifecycle guide.

The project rule records the user's requested division—main-agent planning and
review, execution work through the selected worker with explicit model/effort
parameters—rather than the generic preset's autonomy. Treat static rule
presence as a check result; session loading, actual dispatch, and runtime
worker identity remain unverified or unknown unless independently evidenced. If
the requested execution model or effort is unavailable, report that and ask for
an alternative; never silently fallback or claim execution without evidence.

## Delegation contract

An explicit user-requested division of labor, model, effort, file boundary, or
acceptance criterion takes priority over this skill and over the main agent's
free choice. Do not silently switch profiles, models, efforts, or fallbacks. If
the requested choice is unavailable, say so accurately and ask which alternative
the user wants. When the user gives no additional constraint, retain the main
agent's general autonomy to choose whether and how to delegate.

The main agent owns planning, architecture, review, and final acceptance. An
execution agent works directly within the stated scope; delegation is not a
reason to recurse mechanically into more agents. Pass only the necessary
objective, file-write boundary, interface context, and acceptance criteria, not
the entire conversation history. A successful dispatch and a worker's completed
turn are distinct from the main agent's review and acceptance.

When the host supports it, pass the requested worker `model` and
`reasoning_effort` explicitly and use the actual tool schema. A preset is a
preference, not proof that the host accepted or ran that model.

### Dispatch availability and route selection

Before claiming that dispatch is unavailable, inspect the currently enabled tool
schema or inventory and any documented discovery path. Separate conditions stay
separate: the dispatch tool is absent; the interface rejected the requested
model; a provider, auth, or network error occurred; runtime identity metadata is
missing; or an unrelated business tool (a spreadsheet or browser connection,
for example) is disconnected. None of these implies another, and a stale
runtime snapshot is not an account-wide denial of a model.

Route selection is not a fixed retry or fallback chain, and local static checks
(`list_models`, compatibility preflight, inspection of available routes) do not
contact a provider or prove authentication. A request that was rejected before
dispatch and a job that already started and then failed are different cases:
inspect the existing agent's state and failure reason before any new dispatch,
and do not assume a same-provider failure becomes a success merely because the
work is rerouted through a direct CLI call. No retry, replacement worker, or
broader approval is required by this section. Native dispatch remains allowed
and useful whenever it accepts the requested pair.

### Enabled Delegate Workers MCP interface

Inspect the tools actually exposed in the current session. Use native dispatch
when it accepts the requested pair. When it cannot, and Delegate Workers' own MCP
server `delegate-workers` is enabled and its tools are visible, prefer it over
improvising a CLI invocation: call `list_models` with the project `cwd` to
read the configured preferences and the effective selection source, then
`spawn_agent` with the current user or project model and effort, `get_agent` to
poll status, and `cancel_agent` when the work must stop. `list_models` reports
configured preferences only, not a provider-wide catalog, so absence of a model
there is not evidence that the account lacks it.

Pass the requested pair explicitly when the caller specified a model and effort:
an explicit `model` requires an explicit `reasoning_effort`, and an explicit
`profile` may be used instead. Keep `sandbox` at its `read-only` default unless
the task genuinely edits code or files, and treat `writable_files` as the
handoff's boundary description rather than a per-file enforcement mechanism.
Each `spawn_agent` call is a fresh independent process: the MVP has no resume
tool, so continue work by spawning again with a bounded handoff instead of
assuming an existing session can be reopened. This only avoids the implicit
resume path; it does not guarantee the absence of runtime drift, so consistency
still rests on whatever per-turn metadata the host provides and is unverified
when that metadata is missing. The adapter's `agent_id` is not the backend CLI
session id; report whichever identifiers the tools actually return and keep
requested versus observed values separate.

If the server or its tools are absent from the current session, do not install,
register, or change global settings during an ordinary task; report the concrete
limitation and let the user decide whether to enable it. The exact local Codex
CLI route below remains available within existing authorization.

### Local Codex CLI route

The current user or project selection decides the model and effort; an example
quoted from earlier work never overrides it. A request rejected by native
dispatch only means that host interface will not take it; it does not show the
model is unavailable, so the original id may still be attempted through an
enabled MCP interface or an already available local Codex CLI within existing
authorization. Whether that attempt succeeds depends on the installed CLI
version and provider support, and no route guarantees execution just because it
is tried. Verify the installed CLI's real help and features before using them,
pass the requested model and effort explicitly to the child, and keep the main
session's model and effort unchanged. Do not silently substitute a model or
provider, edit global configuration, request an API key, or install anything
just for this route. A check-only, read-only invocation can probe whether the
route works before real work is assigned.

The MCP adapter and an independent CLI worker are both their own processes, not
native agents. Report the `agent_id` or session id that path actually returns,
if it returns one, and never present a borrowed or parent id as the worker's.
The session header records the requested configuration and a successful response
shows the route worked; neither attests the upstream provider's physical model
identity.

Check that the chosen route actually exposes the tools and files the task needs:
a CLI worker may lack a business tool connection the main session has, such as a
spreadsheet or browser integration. Tool exposure cannot be fabricated,
promised, or created by rewording instructions. Where a task needs retried or
batched transport reads, verify that the intended coverage is complete instead
of assuming success; keep that requirement generic, without hardcoded record
ranges or chunk sizes, and without claiming to fix the upstream tool.

When an explicit division of labor or an applicable project rule requires a
particular model/effort and no authorized route can execute it, pause only the
blocked execution, state the precise limitation, and ask for an alternative. The
main agent may continue planning and run the minimum diagnostics needed to
locate the blocker, but it must not perform the assigned business task under
labels such as "read-only investigation", "retry", or "fallback"; disclosing a
limitation is not authorization to replace the worker. An existing explicit
approval for main-agent takeover can be used without asking again. Reporting
requested versus observed metadata and main-agent work separately does not
itself authorize takeover. "Independent work may continue" means work inside the
already authorized division of labor, not the blocked assignment. Optional
presets keep the main agent's general autonomy: this section adds no scheduler,
fixed retry count, concurrency cap, audit ledger, or new capability file, and
delegation stays non-compulsory for ordinary tasks.

### Worker continuation and model consistency

Treat the selected model/effort as applying to subsequent worker turns, not just
the first spawn. An authorized change of target is a new explicit dispatch
choice; accidental recovery inheritance is not such a choice. A completed turn
is not necessarily a closed agent: a live or
idle worker can continue normally when the host preserves its configuration.
Before reusing a worker after closure, restoration, or a lost connection, check
the actual lifecycle state and the current tool's semantics. A matching agent
ID, an earlier spawn, or a successful `Sent input` response proves neither the
current model nor its reasoning effort. Text in `send_input` / `send_message`
does not set either parameter.

Do not reopen a closed worker through `resume_agent` (or an equivalent follow-up
that implicitly reopens it) when that path cannot explicitly retain/set the
selected pair and provide fresh evidence. Create a new worker with explicit
`model` and `reasoning_effort` instead; use the host's supported minimal-context
fork when overrides cannot be combined with a full-history fork. Hand over only
the objective, current changes, remaining work, file boundary, and relevant test
results. Do not change the main model or global defaults to influence recovery.

When the host exposes runtime metadata, check the first turn and every new
continuation's fresh model/effort record, tied to the actual agent ID and turn
or timestamp. For local session logs, inspect the relevant worker's
`turn_context`, not unrelated sessions or just its initial record. If metadata
appears only after starting a turn, use a check-only handoff before assigning
implementation after a risky reopen/replacement. Missing or stale metadata is
unverified, not a match. If the user requires verification before execution and
the host cannot provide it, explain that limitation and ask for an alternative;
ordinary preset use does not require a new approval or an audit file.

On a recorded mismatch, stop dispatching work to that worker and interrupt/close
it with the available host tool. Confirm it has stopped before giving another
worker the same writable files. If stopping cannot be confirmed, report the
limitation and keep overlapping writes on hold; work inside the already
authorized division of labor may continue. Preserve and review existing changes,
then create a replacement with the intended pair if still available; never
attempt to repair a model mismatch by repeating its name in task text. Report
requested and observed pairs, the affected continuation, and work done by the
main agent separately. A later matching turn does not erase an earlier
mismatch. Session metadata records configuration; it does not independently
attest an upstream provider's physical model identity. These checks add no
fixed retry loop,
concurrency cap, scheduler, or mandatory ledger.

At handoff, report the real agent/thread id only when the tool provides one, the
requested model and effort, changed files, tests run, and any unverified items.
If the host does not report the actual model identity, do not claim that identity
was independently verified.

There is no forced task-record file, fixed handoff format, concurrency policy, or
retry/fallback system imposed here. Default activation is managed separately
through `dw mode`; do not install, update, or change global settings during an
ordinary task unless the user requests it.
