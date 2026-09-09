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
