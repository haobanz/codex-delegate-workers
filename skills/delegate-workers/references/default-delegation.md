## Default Worker Delegation

For coding tasks that benefit from delegation, read the delegate-workers skill at
{skill_path} and its execution model presets without waiting for a skill mention.
Use native subagents when useful, at your discretion.

Keep planning, architecture decisions, and final acceptance in the current main
conversation with its existing model and reasoning effort. Read the skill's current
workers.json and pass the chosen worker model and reasoning effort explicitly.
Do not copy worker settings into the main session or change Codex model settings.

Decide task decomposition, worker selection, parallelism, retries, and follow-up
delegation from the task context. The presets express model preferences, not a fixed
workflow or model allowlist. Subagents may delegate further when useful and supported.
Check the returned work and integrate it in the main conversation.

Answer questions and perform trivial changes directly. Keep unresolved product or
architecture decisions in the main conversation. If delegation is unavailable,
state the concrete limitation and continue in the main conversation when feasible;
never claim that another model ran without a successful dispatch.

Honor an explicit request not to delegate and applicable project instructions.
Do not modify this startup rule as part of ordinary work.
