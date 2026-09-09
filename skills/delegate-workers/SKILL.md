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

At handoff, report the real agent/thread id only when the tool provides one, the
requested model and effort, changed files, tests run, and any unverified items.
If the host does not report the actual model identity, do not claim that identity
was independently verified.

There is no forced task-record file, fixed handoff format, concurrency policy, or
retry/fallback system imposed here. Default activation is managed separately
through `dw mode`; do not install, update, or change global settings during an
ordinary task unless the user requests it.
