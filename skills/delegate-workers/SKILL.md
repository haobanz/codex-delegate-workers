---
name: delegate-workers
description: Delegate bounded implementation, investigation, or validation tasks to configurable Codex worker models while keeping planning and final review in the current conversation. Use for multi-model execution or substantial work that benefits from delegation; small tasks can stay in the main conversation.
---

# Delegate Workers

The current conversation is the coordinator. Its effective model and reasoning
effort are owned by the user's Codex session. Do not configure a planner, require a
specific coordinator model, infer the coordinator from global defaults, or change
the main conversation's model or reasoning effort, including after worker failure.

This skill requests native subagent delegation for suitable bounded execution.
Worker models and reasoning levels come from [workers.json](workers.json), or a
complete config file explicitly supplied by the user. Do not edit any Codex config
or install agent roles as part of performing the user's task.

The installer can enable a managed global startup rule that explicitly applies
this skill before substantial implementation. With that rule active, do not wait
for a skill mention or rely on description matching. The user's request and project
instructions still determine scope. Assigned execution workers remain leaf agents.
The `dw mode` command manages activation separately; never toggle it during an
ordinary coding task. At dispatch time, briefly report the requested worker model
and effort, or the concrete reason for doing the task in the main conversation.

## Select The Work And Worker

Keep requirements, architecture decisions, task decomposition, and final acceptance
in the current conversation. Delegate independently verifiable implementation,
focused investigation, reproduction, or validation when its size justifies a worker.
Keep short commands and trivial edits local when delegation would add more work.
If a task still requires product or architecture decisions, resolve them here first.

1. Run `python3 <skill-dir>/scripts/workers.py show` to read and validate the worker
   profiles. Resolve `<skill-dir>` from this SKILL.md location, regardless of cwd.
   If the user supplied a config, put `--config PATH` before the subcommand on
   every call, including `show`; never mix default and user-supplied profiles.
2. Select the default profile for clear work. A harder but bounded task may start
   with another configured profile; there is no mandatory sequence of models.
3. Run `python3 <skill-dir>/scripts/workers.py resolve` with the selected `--profile`,
   the next `--attempt`, `--active-workers`, and the host's `--available-slots` when
   known.
4. Honor explicit task-specific user choices through `--model`, `--effort`, and
   `--max-parallel`; do not save them as defaults. A model override needs an explicit
   effort; use a supported default exposed by the live host if the user omitted it,
   rather than carrying an effort across models or guessing availability.

The resolver's `delegate` output is a requested worker configuration, not execution
evidence. `wait` means collect or wait for running results before dispatching more
work. `return_to_parent` means reassess in this conversation using the stated reason.

## Dispatch Through Native Tools

Use the host's native subagent spawn tool and pass the resolved `model` and
`reasoning_effort` explicitly. Check the actual tool schema and available model
list. Do not invent parameters, map an unsupported effort to a lower one, or assume
that API availability implies availability in this host's worker tool.

If the host exposes `fork_turns`, use `"none"` with a self-contained work package.
Full-history forks can inherit the parent's model and reject overrides. Otherwise
use the host's documented minimal-context mode that supports explicit overrides.
Do not select a custom agent role with conflicting model settings.

If explicit worker model selection is unavailable or rejected, report the reason
and reassess in the main conversation. Do not launch nested `codex exec`, an API
client, or another transport to work around it. The configured fallback may be
considered explicitly under the failure rules below; never silently substitute it.

Every worker package must include:

- Goal, relevant file paths, and the decisions already made.
- Exact write scope, excluded files, and any dependencies on other workers.
- Acceptance criteria and focused verification commands or observable checks.
- A request to return changed paths, implementation summary, verification results,
  and remaining blockers. The worker must not approve the overall task.
- An instruction to stop and return evidence if the scope is insufficient, and to
  create no further subagents.

Use only permissions already authorized for the task. Workers share the workspace
unless the host says otherwise. Give every modified file one active writer; serialize
overlapping work and integration. Apply the lower of the effective concurrency and
the host's remaining capacity. Main-agent limits govern this skill's workers, not
unrelated work already in progress.

## Validate And Handle Failure

Track each task's worker ID, selected profile, explicit overrides, requested model
and effort, attempts, ownership, and verification result. Count each spawn request
as an attempt, even when rejected. Follow-ups that ask a worker to retry count too;
status queries and waits do not. Pass the next count to the resolver before retries.
The helper does not persist counters or enforce a hard runtime or monetary budget.

Inspect the actual diff and relevant verification output before accepting a result.
Do not equate a worker's completion or claimed PASS with a correct implementation.
Report requested settings separately from runtime-confirmed settings; when runtime
identity is not exposed, say it is unverified rather than asserting it was confirmed.

After failure, distinguish insufficient instructions, environmental failures, and
reasoning difficulty. Clarify or split a poorly scoped task; a stronger worker is
not a remedy for missing requirements or permissions. Before retrying, resolve the
same profile and overrides with the next attempt number. For a justified model
upgrade, use `resolve --escalate-from PROFILE --attempt N` and state the new selection.
If a user pinned a model or effort for this task, preserve it for retries unless
they also allowed fallback; do not use the profile fallback to discard that choice.

On exhausted attempts, no configured fallback, or repeated failure without new
evidence, return the task and evidence to the current coordinator. Keep counting
attempts for the same acceptance criterion; renaming a task does not reset the limit.
Stop the previous worker before reassigning its files, inspect partial edits, and
include them in the handoff. Preserve user and completed worker changes.

The coordinator owns the final outcome and retains its original session settings.
