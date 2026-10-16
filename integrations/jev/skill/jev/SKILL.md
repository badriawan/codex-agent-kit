---
name: jev
description: Use the Jev MCP advisor to choose between existing Codex agent roles or assess a change against its plan and reported validation. Use for ambiguous delegation decisions, meaningful change assessments, or an explicit request to consult Jev.
---

# Jev decision advisor

Use the `jev` MCP server's `route_work` and `review_change` tools. Jev returns
typed judgments and probabilities; the main agent owns orchestration and execution.

## Choose a role

For a useful, nontrivial choice between existing roles, call
`route_work(task_summary)` with a short summary of the task, constraints, and
expected output. Skip trivial tasks and choices already determined by applicable
instructions, including required architect reviews.

Read `status`, `decision.choice`, `decision.confidence`, and
`decision.probabilities` together. `main` means continue in the primary agent;
other choices refer to existing roles, not models. Spawn only when delegation is
authorized, the role is available, and the work benefits from it.

## Assess a change

Call `review_change` when comparing a substantial change with its plan and
validation would help. Supply:

- `plan`: the intended outcome and scope.
- `change_summary`: concise observed changes.
- `validation`: `passed`, `failed`, `not_run`, or `unknown`, based on checks
  actually performed by the caller.
- `outside_plan`: actual paths or effects beyond the plan, if any.
- `irreversible`: whether the change has irreversible effects.

Compute paths and run relevant checks locally first. Jev does not read files or
run tests. Follow `required_next_step`; a positive assessment cannot cancel failed
validation, expand authorization, or replace a required architect review.

## Handle uncertainty and credentials

Send only minimal non-secret summaries. Never send credentials, environment dumps,
complete configuration files, or repository contents to these advisory tools.
Requests use the user's TypeSafe account and may incur API charges.

Treat the configured confidence threshold as provisional, not measured accuracy.
For `uncertain` or `unavailable`, use main-agent judgment and preserve required
reviews. Do not retry merely to obtain a preferred answer.

If the MCP tools are absent, report that Jev is unavailable in this session and
continue the task using the existing workflow. After new installation, start a new
Codex session to load the server and skill. Keep API keys in private per-device
configuration or an environment/secret manager, never in this skill or repository.
