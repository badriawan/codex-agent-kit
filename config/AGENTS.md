# Automatic agent workflow

- Decide delegation from the user's ordinary task; no special agent prompt is required.
- Spawn `architect` to review a large plan before proceeding, when an error repeats, and before declaring a long task complete. Collect its findings and resolve material issues.
- Delegate independent exploration, research, implementation, or validation when doing so materially improves speed or quality. Give each agent a bounded task and expected result; avoid overlapping file edits and collect required results before responding.
- Use `explorer` to inspect existing code, `researcher` to gather evidence, and `worker` to implement focused changes. The main agent coordinates, integrates results, and verifies the outcome.
- Keep trivial tasks in the main agent. Agents are started on demand, not kept running for appearances. Respect the runtime's available concurrency; run work in stages when needed.
- If a required role or delegation tool is unavailable, state the limitation and perform the applicable review in the main agent without claiming that a subagent ran.

## Optional Jev decision tools

- When the `jev` MCP is available, prefer `route_work(task_summary)` for useful, nontrivial choices between existing roles. Skip it for trivial tasks or when a known rule determines the next step; mandatory architect reviews take precedence.
- Use `jev.review_change` for advisory assessment of a substantial change when comparing its plan, reported validation, and remaining uncertainty would help. Compute paths and test outcomes locally. Send compact factual summaries, never secrets or credentials.
- Jev returns advice and probabilities, not permission to execute, merge, publish, or declare completion. Its confidence threshold is provisional. If it is unavailable or uncertain, use main-agent judgment and required architect reviews; do not repeatedly retry.
- Follow deterministic facts, session permissions, and user instructions regardless of a Jev recommendation.
