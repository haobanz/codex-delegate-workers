# Worker Configuration Reference

An original Codex skill for delegating bounded work to configurable worker models.
The current Codex conversation owns planning, decisions, and final acceptance.
Its model and reasoning effort always remain the user's session choices.

## Configuration

Edit [`skills/delegate-workers/workers.json`](../skills/delegate-workers/workers.json).
The defaults are Luna `medium` for normal execution and Terra `high` for harder
bounded work. Profiles can be renamed, added, or changed without editing the skill.
There is no planner model setting.

```json
{
  "version": 1,
  "default_profile": "default",
  "max_parallel_workers": 3,
  "max_attempts_per_task": 2,
  "profiles": {
    "default": {
      "model": "gpt-5.6-luna",
      "reasoning_effort": "medium",
      "fallback": "complex"
    },
    "complex": {
      "model": "gpt-5.6-terra",
      "reasoning_effort": "high"
    }
  }
}
```

Configuration paths are explicit. The tool does not search parent directories,
read `~/.codex/config.toml`, infer the main model, or combine multiple config files.
Use `--config PATH` before the command to select another complete configuration.

## Use In Codex

The source skill is [`skills/delegate-workers/SKILL.md`](../skills/delegate-workers/SKILL.md).
During development, ask Codex to read this file and use it for the task. Once this
folder is installed in a Codex skill directory, invoke it as `$delegate-workers`:

```text
Use $delegate-workers for this change. Keep planning and final review in this
conversation. Use the default worker for implementation, and the complex worker
only when the bounded task needs it.
```

For a task-specific override:

```text
Use $delegate-workers. For this task only, use gpt-5.6-luna with high reasoning
for implementation, with at most two concurrent workers. Keep my session settings.
```

The skill uses native subagent tools with explicit model and reasoning arguments.
It never starts a nested Codex CLI or an external API run as an implicit fallback.
Hosts without explicit worker model selection return the work to the current
conversation and disclose the limitation.

## Configuration Tool

Python 3.10 or newer is sufficient; there are no third-party dependencies.
Run from this repository:

```bash
python3 skills/delegate-workers/scripts/workers.py validate
python3 skills/delegate-workers/scripts/workers.py show
python3 skills/delegate-workers/scripts/workers.py resolve
python3 skills/delegate-workers/scripts/workers.py resolve --profile complex
python3 skills/delegate-workers/scripts/workers.py resolve --effort high
python3 skills/delegate-workers/scripts/workers.py resolve --escalate-from default --attempt 2
```

`resolve` returns JSON with `action` equal to `delegate`, `wait`, or
`return_to_parent`. This is a dispatch plan, not proof that a worker started.
Provide `--active-workers N`, `--available-slots N`, and `--attempt N` from the
current run to check concurrency and attempt limits. Use `--max-parallel N` for a
task-specific concurrency setting. These counts are maintained by the main agent;
the script is not a background scheduler or a hard billing limit.

`--profile`, `--model`, and `--effort` apply to one request without modifying the
configuration. A model override must include an effort override, so a different
model never accidentally receives a profile's reasoning setting. `--escalate-from`
selects that profile's configured fallback and cannot be combined with overrides.
An unsuccessful tool call or an unavailable model must not be reported as execution.

An optional `--capabilities PATH` checks against a normalized model list obtained
from the current host's worker tool. This is our input format, not a Codex API:

```json
{
  "models": [
    {"model": "gpt-5.6-luna", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]}
  ]
}
```

An unavailable combination returns to the parent with a reason. It never silently
substitutes another model or effort. Without this input, availability is explicitly
`unverified`; the main agent must use the live tool's supported values and result.

## Verification

```bash
python3 -m unittest discover -s tests -v
```

Tests cover worker selection, capability mismatches, bounded escalation, per-task
overrides, concurrency, malformed configuration, and preservation of session config.

The install/update menu is a separate user-invoked lifecycle tool. Invoking the
execution skill does not authorize it to install or update itself during a task.
