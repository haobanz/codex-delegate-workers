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
