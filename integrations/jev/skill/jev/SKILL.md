---
name: jev
description: Use Jev for automatic candidate file/tool/action selection, bounded retry decisions, delegation, and change review in Codex. Native hooks check supported local actions when routing is enabled.
---

# Jev decision layer

The main agent plans and executes; Jev selects among supplied candidates. Use the
connected `jev` MCP tools automatically when work reaches these decisions.
Required architect reviews take priority.

## File, tool, and action choices

Use `choose_file(task_summary, candidates)` with candidates containing `id`,
`path`, and `summary`. Paths are relative to the workspace. Use `choose_tool`
with candidates containing `id`, `name`, and `summary`; include only tools that
are actually available. Provide plausible alternatives where there is a choice.

Before a supported local action, call `choose_action` with `session_id`, `cwd`,
`turn_id`, `task_summary`, and `candidates`. The native hook provides the three
scope values when a call first needs routing. Each candidate contains:

- `id`: unique simple identifier, excluding main/retry/change/stop/architect.
- `tool_name`: native hook name such as `Bash`, `apply_patch`, or exact MCP name.
- `input`: actual tool arguments, kept locally and not sent to TypeSafe.
- `files`: relative file paths involved, or an empty array.
- `summary`: concise, non-secret purpose of the action.
- `retry_safe`: true only when replay cannot duplicate external effects or
  unexpectedly change data.

Use native `tool_input.command` for Bash and apply_patch. Execute the selected
candidate through the ordinary tool; the hook checks the routing record. Shell
routing checks the native input observed by Codex, not arbitrary I/O performed
inside a script. Hooks never rewrite commands or patch targets.
Use the tool name and input keys supplied by hook feedback. `max_output_tokens`
and `yield_time_ms` are output transport options and do not affect matching;
execution-affecting options are retained when the runtime exposes them.

For an uncertain/unavailable result or no match, main assesses eligible validated
candidates once. The first selected action consumes the fallback decision group.
If none is eligible, provide a corrected candidate or follow the retry/review
instruction. Never loop on the same blocked call or query Jev for a preferred result.

Candidate selection grants no permission. Respect normal sandbox limits,
external-write authorization, Auto-review, and required architect reviews.

## Failures and retries

PostToolUse records explicit outcomes and automatically selects retry guidance.
Original tool evidence stays available; unknown output is not success. Outside
hook coverage, call `retry_decision` with a short non-secret error summary,
matching failed-attempt count, retry safety, and permission-denied flag.

Some native CLI versions supply only stdout to PostToolUse. When the hook asks,
automatically call `report_outcome` using its exact session/cwd/turn/call IDs and
the actual observed exit code or permission denial from the tool result. Never
infer success from the stdout text. An unknown record can be reconciled once;
the server treats this as main-attested evidence, not independent verification.

- Permission denial stops replay.
- Two matching failed attempts require architect review regardless of confidence.
- Unsafe replay returns to main for a corrected action.
- A retry recommendation still needs a fresh chosen action and normal permissions.

After actual architect review and any necessary repair, use `reviewed_retry` for
one safe retry of the unchanged test command. Supply the candidate, current scope,
real review reference, and non-secret review summary. This main-attested review
is tied to the current failure revision and cannot override permission denial.

Native hooks cover supported local function tools, shell, patches, and MCP tools.
Hosted tools such as web search and ongoing `write_stdin` transport are not
intercepted. Jev and review/control calls are exempt to avoid recursion/deadlock.
Missing servers or hook failures fall back to main; hooks are not a security boundary.

## Delegation and change review

Use `route_work(task_summary)` for useful, nontrivial choices among existing roles.
Skip trivial tasks and choices determined by instructions. Roles are not models.

Use `review_change` for substantial changes with the plan, observed change summary,
actual validation (`passed`, `failed`, `not_run`, `unknown`), outside-plan effects,
and irreversibility. Follow its deterministic next step. It reads no files and
runs no tests; positive advice cannot cancel failed validation or required reviews.

## Data and setup

Only send short non-secret task/candidate/error summaries. Never put credentials,
raw commands, patch contents, output, full configuration, or environment dumps into
summary fields. Candidate `input` remains local. Probabilities and thresholds are
provisional advice, not measured accuracy or authorization.

Each device needs its own API key and Python environment. Enable routing through
`configure_runtime.py install --jev`, review/trust the exact hooks using `/hooks`,
and start a new session. Native Auto-review is enabled separately with
`--auto-review`; Jev never approves permission requests. If tools are missing,
report that limitation and continue using main's judgment and required reviews.
