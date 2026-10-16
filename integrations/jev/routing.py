"""Local routing gates, never permission grants or tool execution.

Only candidate summaries reach Jev. Arguments and outputs stay local; SQLite
contains hashes, native call identifiers, and bounded-lived counters. Observed
execution options must match; output transport options are ignored. A hook
cannot validate execution options that its runtime does not expose.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import decisions

CONTROL_TOOLS = {
    "spawn_agent", "Agent", "send_message", "followup_task", "interrupt_agent",
    "wait_agent", "list_agents", "request_user_input", "request_user_input_async",
}


def exempt(name):
    leaf = name.rsplit(".", 1)[-1]
    native_control = name.removeprefix("collaboration")
    return (name.startswith("mcp__jev__") or leaf in CONTROL_TOOLS
            or native_control in CONTROL_TOOLS)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def fingerprint(name, arguments):
    if name in {"Bash", "exec_command", "shell"}:
        name = "Bash"
        if isinstance(arguments, dict):
            arguments = dict(arguments)
            # These control output transport, not the operation or its scope.
            # Native Bash hooks do not expose them. Never drop execution context.
            for key in ("max_output_tokens", "yield_time_ms"):
                arguments.pop(key, None)
            if "cmd" in arguments and "command" not in arguments:
                arguments["command"] = arguments.pop("cmd")
    if name == "apply_patch" and isinstance(arguments, str):
        arguments = {"command": arguments}
    return _hash([name, arguments])


def outcome(response):
    """Aggregate explicit evidence with denied > failed > passed > unknown.

    MCP protocol success alone is not evidence that the underlying action passed.
    """
    found = set()

    def inspect(value, depth=0, trusted=True):
        if depth > 12:
            return
        if isinstance(value, dict):
            status = value.get("status")
            if trusted and (value.get("permission_denied") is True or status in ("denied", "rejected")):
                found.add("denied")
            if value.get("isError") is True or value.get("is_error") is True or status in ("failed", "error"):
                found.add("failed")
            code = value.get("exit_code")
            if type(code) is int:
                found.add("passed" if code == 0 else "failed")
            if status in ("passed", "success", "succeeded", "completed"):
                found.add("passed")
            for key in ("structuredContent", "structured_content", "content", "result", "text"):
                if key in value:
                    inspect(value[key], depth + 1, trusted and key not in ("content", "text"))
        elif isinstance(value, list):
            for entry in value:
                inspect(entry, depth + 1, trusted)
        elif isinstance(value, str) and depth > 0:
            # MCP text may carry structured domain results, but stdout text can
            # never declare a permission denial. Bare native hook strings are
            # stdout only and cannot establish an action's exit status.
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                inspect(parsed, depth + 1, False)
    inspect(response)
    return next((value for value in ("denied", "failed", "passed") if value in found), "unknown")


class RouteStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("JEV_STATE_PATH", "") or
                         Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) /
                         ".agent-kit/jev-routing.sqlite")

    @contextmanager
    def connect(self):
        if any(p.is_symlink() for p in [self.path, *self.path.parents]):
            raise ValueError("Linked state path")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            connection.execute("PRAGMA busy_timeout=5000")
            # Versioned tables deliberately cannot reuse old unscoped grants.
            connection.execute("CREATE TABLE IF NOT EXISTS route_groups_v2 (id TEXT PRIMARY KEY, scope TEXT, failure_scope TEXT, expires REAL)")
            connection.execute("CREATE TABLE IF NOT EXISTS route_grants_v2 (group_id TEXT, signature TEXT, safe INTEGER, revision INTEGER, PRIMARY KEY(group_id,signature))")
            connection.execute("CREATE TABLE IF NOT EXISTS route_calls_v2 (scope TEXT, id TEXT, signature TEXT, safe INTEGER, status TEXT, created REAL, revision INTEGER, failure_scope TEXT, PRIMARY KEY(scope,id))")
            if "turn_id" not in {r[1] for r in connection.execute("PRAGMA table_info(route_calls_v2)")}:
                connection.execute("ALTER TABLE route_calls_v2 ADD COLUMN turn_id TEXT DEFAULT ''")
            connection.execute("CREATE TABLE IF NOT EXISTS route_failures_v2 (scope TEXT, signature TEXT, count INTEGER, revision INTEGER, updated REAL, denied INTEGER, PRIMARY KEY(scope,signature))")
            connection.execute("CREATE TABLE IF NOT EXISTS route_reviews_v2 (scope TEXT, signature TEXT, revision INTEGER, reference TEXT, created REAL, PRIMARY KEY(scope,signature,revision,reference))")
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            connection.execute("DELETE FROM route_groups_v2 WHERE expires < ?", (now,))
            connection.execute("DELETE FROM route_grants_v2 WHERE group_id NOT IN (SELECT id FROM route_groups_v2)")
            connection.execute("DELETE FROM route_calls_v2 WHERE created < ?", (now - 86400,))
            connection.execute("DELETE FROM route_failures_v2 WHERE updated < ?", (now - 86400,))
            connection.execute("DELETE FROM route_reviews_v2 WHERE created < ?", (now - 86400,))
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def scopes(session, cwd="", turn_id=""):
        # Resolve workspace aliases locally, but persist only its digest.
        workspace = str(Path(cwd).resolve()) if cwd else ""
        return _hash([session, workspace, turn_id]), _hash([session, workspace])

    def state(self, session, signature, cwd=""):
        _, scope = self.scopes(session, cwd)
        with self.connect() as db:
            row = db.execute("SELECT count,denied,revision FROM route_failures_v2 WHERE scope=? AND signature=?", (scope, signature)).fetchone()
            return row if row else (0, 0, 0)

    def failures(self, session, signature, cwd=""):
        return self.state(session, signature, cwd)[0]

    def denied(self, session, signature, cwd=""):
        return bool(self.state(session, signature, cwd)[1])

    def grant(self, session, candidates, cwd="", turn_id="", expected_revisions=None):
        scope, failure_scope = self.scopes(session, cwd, turn_id)
        with self.connect() as db:
            prepared = []
            for candidate in candidates:
                signature = fingerprint(candidate["tool_name"], candidate["input"])
                row = db.execute("SELECT count,denied,revision FROM route_failures_v2 WHERE scope=? AND signature=?", (failure_scope, signature)).fetchone() or (0, 0, 0)
                unresolved = db.execute("SELECT 1 FROM route_calls_v2 WHERE failure_scope=? AND signature=? AND status='unknown'", (failure_scope, signature)).fetchone()
                if unresolved or row[1] or (expected_revisions is None and row[0]):
                    continue
                if expected_revisions is not None and expected_revisions.get(signature) != row[2]:
                    continue
                prepared.append((signature, int(candidate.get("retry_safe") is True), row[2]))
            return self._issue_group(db, scope, failure_scope, prepared)

    @staticmethod
    def _issue_group(db, scope, failure_scope, prepared):
        if not prepared:
            return False
        # Equivalent candidate inputs share one local reservation. If their
        # safety attestations conflict, retain the conservative unsafe value.
        unique = {}
        for signature, safe, revision in prepared:
            previous = unique.get(signature)
            unique[signature] = (signature, min(safe, previous[1]) if previous else safe, revision)
        prepared = list(unique.values())
        # Replace overlapping decisions, preserving unrelated parallel actions.
        old = set()
        for signature, _, _ in prepared:
            old.update(r[0] for r in db.execute(
                "SELECT d.id FROM route_groups_v2 d JOIN route_grants_v2 g ON g.group_id=d.id WHERE d.scope=? AND g.signature=?",
                (scope, signature)))
        for group in old:
            db.execute("DELETE FROM route_grants_v2 WHERE group_id=?", (group,))
            db.execute("DELETE FROM route_groups_v2 WHERE id=?", (group,))
        group = secrets.token_hex(16)
        db.execute("INSERT INTO route_groups_v2 VALUES(?,?,?,?)", (group, scope, failure_scope, time.time() + 600))
        db.executemany("INSERT INTO route_grants_v2 VALUES(?,?,?,?)", [(group, *row) for row in prepared])
        return True

    def reviewed_grant(self, session, candidate, review_reference, cwd="", turn_id=""):
        """Consume an attested review once without resetting failure evidence."""
        scope, failure_scope = self.scopes(session, cwd, turn_id)
        signature = fingerprint(candidate["tool_name"], candidate["input"])
        with self.connect() as db:
            row = db.execute("SELECT count,denied,revision FROM route_failures_v2 WHERE scope=? AND signature=?", (failure_scope, signature)).fetchone()
            unresolved = db.execute("SELECT 1 FROM route_calls_v2 WHERE failure_scope=? AND signature=? AND status='unknown'", (failure_scope, signature)).fetchone()
            if unresolved:
                return "report_outcome_required"
            if row and row[1]:
                return "permission_denied"
            if not row or row[0] < 2:
                return "review_not_required"
            reference = _hash(review_reference)
            old = db.execute("SELECT 1 FROM route_reviews_v2 WHERE scope=? AND signature=? AND revision=? AND reference=?", (failure_scope, signature, row[2], reference)).fetchone()
            if old:
                return "review_already_used"
            db.execute("INSERT INTO route_reviews_v2 VALUES(?,?,?,?,?)", (failure_scope, signature, row[2], reference, time.time()))
            self._issue_group(db, scope, failure_scope, [(signature, 1, row[2])])
            return "selected"

    def reserve(self, session, call_id, signature, cwd="", turn_id=""):
        scope, failure_scope = self.scopes(session, cwd, turn_id)
        with self.connect() as db:
            state = db.execute("SELECT revision FROM route_failures_v2 WHERE scope=? AND signature=?", (failure_scope, signature)).fetchone()
            revision = state[0] if state else 0
            old = db.execute("SELECT signature,status,revision FROM route_calls_v2 WHERE scope=? AND id=?", (scope, call_id)).fetchone()
            if old:
                return old == (signature, "pending", revision)
            if db.execute("SELECT 1 FROM route_calls_v2 WHERE failure_scope=? AND signature=? AND status='unknown'", (failure_scope, signature)).fetchone():
                return False
            grant = db.execute("SELECT g.group_id,g.safe,g.revision FROM route_grants_v2 g JOIN route_groups_v2 d ON d.id=g.group_id WHERE d.scope=? AND g.signature=?", (scope, signature)).fetchone()
            if grant is None or grant[2] != revision:
                return False
            db.execute("DELETE FROM route_grants_v2 WHERE group_id=?", (grant[0],))
            db.execute("DELETE FROM route_groups_v2 WHERE id=?", (grant[0],))
            db.execute("INSERT INTO route_calls_v2(scope,id,signature,safe,status,created,revision,failure_scope,turn_id) VALUES(?,?,?,?,?,?,?,?,?)", (scope, call_id, signature, grant[1], "pending", time.time(), revision, failure_scope, turn_id))
            return True

    def complete(self, session, call_id, signature, status, cwd="", turn_id="", reconcile=False):
        scope, failure_scope = self.scopes(session, cwd, turn_id)
        with self.connect() as db:
            row = db.execute("SELECT signature,safe,status,revision FROM route_calls_v2 WHERE scope=? AND id=?", (scope, call_id)).fetchone()
            if not row or row[0] != signature or row[2] != ("unknown" if reconcile else "pending"):
                return None
            db.execute("UPDATE route_calls_v2 SET status=? WHERE scope=? AND id=?", (status, scope, call_id))
            if status in {"failed", "denied"}:
                db.execute("INSERT INTO route_failures_v2 VALUES(?,?,1,1,?,?) ON CONFLICT(scope,signature) DO UPDATE SET count=count+1,revision=revision+1,updated=excluded.updated,denied=max(route_failures_v2.denied,excluded.denied)", (failure_scope, signature, time.time(), int(status == "denied")))
                groups = [r[0] for r in db.execute("SELECT DISTINCT d.id FROM route_groups_v2 d JOIN route_grants_v2 g ON g.group_id=d.id WHERE d.failure_scope=? AND g.signature=?", (failure_scope, signature))]
                for group in groups:
                    db.execute("DELETE FROM route_grants_v2 WHERE group_id=?", (group,))
                    db.execute("DELETE FROM route_groups_v2 WHERE id=?", (group,))
            elif status == "passed":
                # A success started before another call failed cannot erase it.
                db.execute("UPDATE route_failures_v2 SET count=0,updated=? WHERE scope=? AND signature=? AND revision=? AND denied=0", (time.time(), failure_scope, signature, row[3]))
            count = db.execute("SELECT count FROM route_failures_v2 WHERE scope=? AND signature=?", (failure_scope, signature)).fetchone()
            return {"retry_safe": bool(row[1]), "attempts": count[0] if count else 0}

    def unresolved(self, session, signature, cwd=""):
        _, failure_scope = self.scopes(session, cwd)
        with self.connect() as db:
            return db.execute("SELECT id,turn_id FROM route_calls_v2 WHERE failure_scope=? AND signature=? AND status='unknown' ORDER BY created LIMIT 1", (failure_scope, signature)).fetchone()

    def reconcile(self, session, call_id, status, cwd="", turn_id=""):
        scope, _ = self.scopes(session, cwd, turn_id)
        with self.connect() as db:
            row = db.execute("SELECT signature FROM route_calls_v2 WHERE scope=? AND id=? AND status='unknown'", (scope, call_id)).fetchone()
        if not row:
            return None
        # complete repeats its status check atomically, so concurrent reports
        # cannot double-count a failure or overwrite a recorded result.
        return self.complete(session, call_id, row[0], status, cwd, turn_id, reconcile=True)


def context(event, message, deny=False):
    specific = {"hookEventName": event, "additionalContext": message}
    if deny:
        specific.update(permissionDecision="deny", permissionDecisionReason=message)
    return {"hookSpecificOutput": specific}


def valid_identity(session, call_id="unused"):
    return all(isinstance(x, str) and re.fullmatch(r"[\w.:-]{1,200}", x) for x in (session, call_id))


def valid_scope(cwd, turn_id):
    return (isinstance(cwd, str) and len(cwd) <= 4096 and "\x00" not in cwd
            and (not cwd or Path(cwd).is_absolute())
            and isinstance(turn_id, str) and len(turn_id) <= 200)


async def select_action(session_id, task_summary, candidates, choose, retry, store, cwd="", turn_id=""):
    if not valid_identity(session_id) or not valid_scope(cwd, turn_id):
        return {"disposition": "main", "reason": "invalid_routing_identity", "next_step": "replan"}
    result = await choose(task_summary, candidates)
    valid = candidates if decisions._metadata(candidates, "action") is not None else []
    selected = result.get("selected_candidate")
    if result.get("disposition") != "selected" or not selected or selected not in valid:
        if not valid or not store.grant(session_id, valid, cwd, turn_id):
            return {**result, "disposition": "main", "selected_candidate": None, "next_step": "replan", "reason": "no_eligible_candidates"}
        return {**result, "disposition": "main", "selected_candidate": None, "next_step": "Main chooses one eligible candidate; the first execution consumes this decision group. Permissions still apply."}
    signature = fingerprint(selected["tool_name"], selected["input"])
    unresolved = store.unresolved(session_id, signature, cwd)
    if unresolved:
        return {"disposition": "main", "selected_candidate": None,
                "next_step": "report_outcome", "reason": "prior_action_outcome_unknown",
                "report_arguments": {"session_id": session_id, "cwd": cwd,
                                     "turn_id": unresolved[1], "tool_use_id": unresolved[0]}}
    count, denied, revision = store.state(session_id, signature, cwd)
    if denied:
        return {"disposition": "stop", "next_step": "stop", "selected_candidate": None,
                "reason": "permission_denied"}
    if count >= 2:
        return {"disposition": "architect", "next_step": "architect", "selected_candidate": None,
                "reason": "repeated_error_requires_review"}
    if count and selected.get("retry_safe") is not True:
        return {"disposition": "main", "next_step": "replan", "selected_candidate": None,
                "reason": "unsafe_to_retry"}
    if count:
        decision = await retry(task_summary, "The same action previously returned an explicit failure.", count, selected.get("retry_safe") is True, bool(denied))
        if decision.get("next_step") != "retry":
            return {**decision, "selected_candidate": None, "reason": "previous_action_failed"}
    if not store.grant(session_id, [selected], cwd, turn_id, {signature: revision}):
        return {"disposition": "main", "selected_candidate": None, "next_step": "replan", "reason": "failure_state_changed"}
    return result


async def reviewed_retry(session_id, cwd, turn_id, candidate, review_reference, review_summary, store):
    """Register one safe action after a main-attested architect review/source fix.

    The reference and summary are attestations, not proof supplied by this tool.
    Neither leaves this process; only the reference hash is persisted. This does
    not execute the action, approve it, or erase previous failure evidence.
    """
    if (not valid_identity(session_id) or not valid_scope(cwd, turn_id)
            or decisions._metadata([candidate], "action") is None
            or candidate.get("retry_safe") is not True
            or not decisions._text(review_reference, 200)
            or not decisions._text(review_summary, 1500)):
        return {"disposition": "main", "next_step": "replan", "reason": "invalid_review_attestation"}
    result = store.reviewed_grant(session_id, candidate, review_reference, cwd, turn_id)
    if result == "selected":
        return {"disposition": "selected", "selected_candidate": candidate,
                "next_step": "main_agent_permission_and_execution_checks",
                "reason": "reviewed_retry_registered", "review_attested_by": "main",
                "advisory_only": True}
    return {"disposition": "stop" if result == "permission_denied" else "main",
            "next_step": "stop" if result == "permission_denied" else "replan", "reason": result}


async def report_outcome(session_id, cwd, turn_id, tool_use_id, exit_code,
                         permission_denied, store, retry):
    """Reconcile one unknown call using exit/denial evidence attested by main.

    This tool does not inspect raw output or prove the caller's assertion. It
    cannot overwrite an explicit result or reconcile an already reported call.
    """
    if (not valid_identity(session_id, tool_use_id) or not valid_scope(cwd, turn_id)
            or not isinstance(permission_denied, bool)
            or (exit_code is not None and type(exit_code) is not int)
            or (exit_code is None and not permission_denied)):
        return {"disposition": "main", "next_step": "collect_outcome_evidence", "reason": "invalid_outcome_attestation"}
    status = "denied" if permission_denied else ("passed" if exit_code == 0 else "failed")
    record = store.reconcile(session_id, tool_use_id, status, cwd, turn_id)
    if record is None:
        return {"disposition": "main", "next_step": "main", "reason": "no_unresolved_matching_call"}
    if status == "passed":
        decision = {"disposition": "main", "next_step": "main"}
    elif status == "denied":
        decision = {"disposition": "stop", "next_step": "stop"}
    elif record["attempts"] >= 2:
        decision = {"disposition": "architect", "next_step": "architect"}
    elif not record["retry_safe"]:
        decision = {"disposition": "main", "next_step": "main"}
    else:
        decision = await retry("Choose a next step after the main agent reported a native tool failure.",
                               "Main attests an observed nonzero exit status; raw output stays local.",
                               record["attempts"], record["retry_safe"], False)
    return {**decision, "recorded_outcome": status, "outcome_attested_by": "main",
            "attempts": record["attempts"], "advisory_only": True}


def pre_tool(session_id, tool_use_id, tool_name, tool_input, store, cwd="", turn_id=""):
    if exempt(tool_name):
        return {}
    if not valid_identity(session_id, tool_use_id) or not valid_scope(cwd, turn_id):
        return context("PreToolUse", "Jev routing identity unavailable; main must assess this action. Native approvals still apply.")
    if store.reserve(session_id, tool_use_id, fingerprint(tool_name, tool_input), cwd, turn_id):
        return context("PreToolUse", "Jev candidate routing matched. Continue through normal sandbox and Auto-review checks.")
    args = json.dumps({"session_id": session_id, "cwd": cwd, "turn_id": turn_id}, ensure_ascii=False)
    observed = json.dumps({"tool_name": tool_name,
                           "input_keys": sorted(tool_input) if isinstance(tool_input, dict) else []})
    return context("PreToolUse", "Call mcp__jev__choose_action before this local tool with these exact scope arguments: " + args + ". Native input shape: " + observed + ". "
                   "Also supply task_summary and candidates containing id, tool_name, input, files, summary, retry_safe. "
                   "Use the native tool name and input keys above; Bash command and exec_command cmd normalize. Output transport fields max_output_tokens/yield_time_ms are ignored; execution context is not. "
                   "Command-only hooks require command-only candidates. Scope cannot be reused in another workspace or turn. "
                   "Only non-secret summaries go to Jev. Execute one exact selected/main fallback candidate. "
                   "If no eligible candidates remain, replan. Do not repeat a blocked call without a decision. "
                   "Architect/control and Jev calls are exempt.", True)


async def post_tool(session_id, tool_use_id, tool_name, tool_input, tool_response, store, retry, cwd="", turn_id=""):
    if exempt(tool_name) or not valid_identity(session_id, tool_use_id) or not valid_scope(cwd, turn_id):
        return {}
    status = outcome(tool_response)
    record = store.complete(session_id, tool_use_id, fingerprint(tool_name, tool_input), status, cwd, turn_id)
    if record is None:
        return {}
    if status == "unknown":
        args = json.dumps({"session_id": session_id, "cwd": cwd, "turn_id": turn_id,
                           "tool_use_id": tool_use_id}, ensure_ascii=False)
        return context("PostToolUse", "Jev recorded an unknown outcome: native hooks can expose stdout only. "
                       "Automatically call mcp__jev__report_outcome with these exact arguments: " + args +
                       ". Also supply the exit_code observed in the original tool result, or permission_denied=true "
                       "only for an actual native denial. This is a main-agent attestation. Do not infer status from stdout. "
                       "If status evidence is missing, collect it before retrying the same action. No automatic replay.")
    if status not in {"failed", "denied"}:
        return context("PostToolUse", f"Jev recorded tool outcome: {status}. Original evidence is preserved.")
    if status == "denied":
        decision = {"next_step": "stop"}
    elif record["attempts"] >= 2:
        decision = {"next_step": "architect"}
    elif not record["retry_safe"]:
        decision = {"next_step": "main"}
    else:
        decision = await retry("Choose a next step after this local tool failure.", "The tool returned an explicit failure; raw output stays local.", record["attempts"], record["retry_safe"], False)
    return context("PostToolUse", "Jev retry decision: " + str(decision.get("next_step", "main")) +
                   ". No automatic command replay. A repeated matching failure requires architect review; "
                   "choose a corrected action before continuing.")
