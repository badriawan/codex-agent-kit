# Automatic agent workflow

- Decide delegation from the user's ordinary task; no special agent prompt is required.
- Spawn `architect` before producing a large plan, when the same error repeats, and before declaring a long-running task complete. Collect its findings and resolve material issues.
- Delegate independent exploration, research, implementation, or validation when doing so materially improves speed or quality. Give each agent a bounded task and expected result; avoid overlapping file edits and collect required results before responding.
- Use `explorer` to inspect existing code, `researcher` to gather evidence, and `worker` to implement focused changes and run proportionate checks. The main agent plans, writes code when appropriate, coordinates, integrates results, checks the reported evidence, and verifies the outcome before declaring completion.
- Keep trivial tasks in the main agent. Agents are started on demand, not kept running for appearances. Run at most three subagents concurrently, for four agents including the primary, and respect a lower runtime limit. The architect shares those slots; stage reviews when needed. Unless the user explicitly asks, subagents must not delegate further.
- If a required role or delegation tool is unavailable, state the limitation and perform the applicable review in the main agent without claiming that a subagent ran.

## Jev role-only delegation

- Primary defines the task, scope, and eligible available roles. For a useful nontrivial delegation choice, call `jev.route_work(task_summary, available_roles)` once with a short non-secret summary and only canonical roles actually available in this session. Keep trivial work in primary; mandatory architect checkpoints invoke architect directly.
- The role-only MCP validates the response format, confidence, and selected role against the supplied list. It cannot independently discover live roles or reserve concurrency slots. Confidence is provisional, not measured correctness or authorization.
- When the result has `validated=true`, `disposition=selected`, and `agent_type` matching a supplied role, primary forwards that role to native `spawn_agent` with a complete bounded task. Do not repeat the semantic role assessment. Keep the custom role's locked model and reasoning effort; runtime permissions, task authorization, and concurrency limits still apply. Replan if availability or task scope changed.
- A `main`, uncertain, invalid, unavailable, or missing-tool result returns the choice to primary once. Do not loop on Jev. Primary owns integration, actual verification, and the final answer.
- Jev is used only to suggest subagent roles. Do not use its legacy file/tool/action selection, retry, change-review, routing-context, or hook tools. Role-only mode requires no Jev action hooks; Auto-review remains separate.
- Never send secrets, source files, raw commands, output, full prompts, transcripts, or configuration to Jev. Only the short task summary and role metadata are sent to TypeSafe. Jev neither spawns agents nor grants permission.
