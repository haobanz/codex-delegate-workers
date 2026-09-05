---
name: delegate-workers
description: Use configured model and reasoning presets when delegating work in Codex. The current main agent decides task decomposition, worker selection, parallelism, and follow-up work while preserving its own session settings.
---

# Delegate Workers

Keep the main conversation's model and reasoning effort exactly as selected in
Codex. This skill provides execution model presets, not a scheduling policy.

Read [workers.json](workers.json), or run
`<python> <skill-dir>/scripts/workers.py show`, where `<skill-dir>` is this file's
directory. Use `python3` on Linux/macOS, or `py -3` (or `python`) on Windows.
A user-supplied config can be read with `--config PATH` before `show`.
For a selected preset, `resolve --profile NAME` returns its model and effort.
The default preset is the user's preferred starting point; other presets are
available when their capabilities better fit the work. These are preferences,
not an exclusive model allowlist. Respect explicit task-specific user choices.

The main agent decides whether to delegate, how to break down the task, which
model to use, how many agents to run, whether to retry, and when to change approach.
Subagents may delegate further when useful and supported by the host. There is no
fixed retry count, concurrency cap, fallback chain, mandatory task record, or fixed
handoff format imposed by this skill. Use the host's available capacity and normal
collaboration tools, and coordinate shared edits as the task requires.

When creating an execution subagent, pass the chosen `model` and `reasoning_effort`
explicitly when the native tool supports them. If the tool's full-history fork
inherits the main model and rejects overrides, use a supported minimal-context fork
with the context the worker needs. Use the actual tool schema rather than assuming
parameter names or available models.

The main agent retains planning and final integration, and checks the work returned
by subagents. A selected preset is a request, not proof of which model ran; report
capability failures or unknown runtime identity accurately and decide the next step
from the task context. Do not change the main model to match a worker preset.

Default activation is managed separately through `dw mode`. Do not install, update,
or change global settings while doing an ordinary task unless the user requests it.
