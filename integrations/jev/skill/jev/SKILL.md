---
name: jev
description: Ask Jev to suggest an available Codex subagent role for a nontrivial delegation choice. Code validates the role; primary invokes the agent with its locked model.
---

# Jev role-only delegation

Primary owns the task, scope, execution, integration, and final verification.
Jev suggests only a role; the MCP server validates the structured recommendation.

## Use the recommendation

1. Keep simple tasks in primary. Invoke architect directly at mandatory review
   checkpoints. For other useful delegation choices, list only the canonical
   roles actually available and eligible in this session: `explorer`,
   `researcher`, `worker`, `architect`.
2. Call the connected `jev.route_work` once with `task_summary` and
   `available_roles`. Send a brief non-secret summary, not files, commands,
   output, full prompts, transcripts, credentials, or configuration.
3. A usable result has `validated=true`, `disposition=selected`, and `agent_type`
   matching a supplied role. Forward that role to native `spawn_agent` with the
   bounded task and expected output. Do not redo semantic role selection. Keep
   the custom role's fixed model and reasoning effort. Runtime permissions,
   task authorization, and concurrency limits still apply; replan if the task
   or availability changed.
4. On `main`, uncertainty, invalid output, unavailable API, or missing MCP tool,
   primary chooses once and continues. Do not retry Jev to seek another answer.

The server checks the caller's role list; it cannot independently discover live
roles, reserve a slot, spawn an agent, or grant execution permission. The
confidence threshold is provisional and does not establish correctness.

## Installed mode

Role-only mode exposes only `route_work` through `role_server.py`. It uses no
Jev action hooks, routing context, file/tool/action selection, retry decisions,
or change review. Auto-review is independent. If legacy tools are still shown,
do not use them; report that the session needs to reload the role-only server.

Configure with `configure_runtime.py install --jev-roles-only --jev-python`
using the device's absolute Python path, then start a new session to load the
new MCP and skill metadata. Each device needs its own TypeSafe credentials.
