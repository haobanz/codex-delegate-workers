## Default Worker Delegation

For substantial coding tasks, apply the delegate-workers skill at {skill_path}
before implementation. This is an explicit instruction to use native subagents for
bounded execution; do not wait for the user to mention the skill.

Keep planning, architecture decisions, and final acceptance in the current main
conversation with its existing model and reasoning effort. Read the skill's current
workers.json and pass the chosen worker model and reasoning effort explicitly.
Do not copy worker settings into the main session or change Codex model settings.

Once scope and acceptance criteria are clear, delegate substantial independently
verifiable implementation to an execution worker. Before starting, briefly report
the requested worker model and effort. Inspect the returned changes and verification
evidence yourself. Follow the skill's ownership, concurrency, and attempt limits.

Answer questions and perform trivial changes directly. Keep unresolved product or
architecture decisions in the main conversation. If delegation is unavailable,
state the concrete limitation and continue in the main conversation when feasible;
never claim that another model ran without a successful dispatch.

If you are already a worker assigned a bounded task, execute that task without
creating subagents. Honor an explicit request not to delegate and applicable
project instructions. Do not modify this startup rule as part of ordinary work.
