"""Native hook contract tests with local state and a mocked decision provider."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import decisions
import routing


def candidate(command="python -m unittest", safe=True):
    return {"id": "tests", "tool_name": "Bash", "input": {"command": command},
            "files": [], "summary": "Run local tests", "retry_safe": safe}


class Hooks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = routing.RouteStore(Path(self.temp.name).resolve() / "state.sqlite")
        self.action = candidate()
        self.signature = routing.fingerprint("Bash", self.action["input"])
        self.choose = AsyncMock(return_value={"disposition": "selected", "selected_candidate": self.action})
        self.retry = AsyncMock(return_value={"next_step": "retry", "disposition": "main"})

    def tearDown(self):
        self.temp.cleanup()

    async def register(self, action=None):
        return await routing.select_action("session", "Run the appropriate tests", [action or self.action], self.choose, self.retry, self.store)

    async def finish(self, identifier, response):
        return await routing.post_tool("session", identifier, "Bash", self.action["input"], response, self.store, self.retry)

    def pre(self, identifier="call", command=None):
        return routing.pre_tool("session", identifier, "Bash", {"command": command or self.action["input"]["command"]}, self.store)

    async def test_missing_selection_denies_once_with_recovery_instructions(self):
        result = self.pre()["hookSpecificOutput"]
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("choose_action", result["additionalContext"])
        self.assertNotIn("python -m unittest", json.dumps(result))

    async def test_selected_action_matches_without_granting_permissions(self):
        await self.register()
        result = self.pre()["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", result)
        self.assertEqual(self.pre(), {"hookSpecificOutput": result})  # same hook event is idempotent
        self.assertEqual(self.pre("other")["hookSpecificOutput"]["permissionDecision"], "deny")
        await self.finish("call", {"exit_code": 0})
        self.assertEqual(self.pre()["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_different_tool_or_arguments_cannot_reuse_choice(self):
        await self.register()
        self.assertEqual(self.pre(command="python unrelated.py")["hookSpecificOutput"]["permissionDecision"], "deny")
        result = routing.pre_tool("session", "different", "apply_patch", self.action["input"], self.store)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_safe_normalization_and_recursion_control_exemptions(self):
        self.assertEqual(self.signature, routing.fingerprint("exec_command", {"cmd": "python -m unittest"}))
        self.assertEqual(self.signature, routing.fingerprint("exec_command", {"cmd": "python -m unittest", "max_output_tokens": 20, "yield_time_ms": 1000}))
        for field in ("workdir", "env", "shell", "login", "tty", "sandbox_permissions"):
            self.assertNotEqual(self.signature, routing.fingerprint("exec_command", {"cmd": "python -m unittest", field: "different"}))
        for name in ("mcp__jev__choose_action", "spawn_agent", "collaboration.send_message", "collaborationspawn_agent"):
            self.assertEqual(routing.pre_tool("session", "id", name, {}, self.store), {})
        self.assertFalse(routing.exempt("collaborationspawn_agent_extra"))
        self.assertFalse(routing.exempt("othercollaborationspawn_agent"))
        self.assertFalse(self.store.path.exists())

    async def test_concurrent_calls_consume_one_selection(self):
        await self.register()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda name: self.store.reserve("session", name, self.signature), ["a", "b"]))
        self.assertEqual(sorted(results), [False, True])

    async def test_uncertainty_grants_valid_main_fallback_without_model_loop(self):
        self.choose.return_value = {"disposition": "main", "reason": "uncertain"}
        result = await self.register()
        self.assertIn("Main chooses", result["next_step"])
        self.assertNotIn("permissionDecision", self.pre()["hookSpecificOutput"])

    async def test_invalid_candidate_fallback_registers_nothing(self):
        self.choose.return_value = {"disposition": "main"}
        await self.register({"id": "bad"})
        self.assertEqual(self.pre()["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_failure_is_deduplicated_and_evidence_retained(self):
        await self.register()
        self.pre()
        result = await self.finish("call", {"exit_code": 1, "output": "SECRET raw output"})
        self.assertNotIn("decision", result)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.store.failures("session", self.signature), 1)
        self.assertEqual(await self.finish("call", {"exit_code": 1}), {})
        self.assertEqual(self.store.failures("session", self.signature), 1)
        self.assertNotIn("SECRET", str(self.retry.call_args))

    async def test_unknown_response_does_not_clear_failure(self):
        await self.register()
        self.pre()
        await self.finish("call", {"exit_code": 1})
        await self.register()
        self.pre("second")
        await self.finish("second", {"output": "Looks good"})
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_two_matching_failures_require_architect_even_when_provider_positive(self):
        async def retry(*args):
            return await decisions.retry_decision(*args, evaluate=AsyncMock())
        # First safe retry is explicitly selected by our mocked provider.
        for identifier in ("first", "second"):
            await self.register()
            self.pre(identifier)
            await self.finish(identifier, {"exit_code": 1})
        result = await routing.select_action("session", "Run tests", [self.action], self.choose, retry, self.store)
        self.assertEqual(result["disposition"], "architect")
        self.assertEqual(self.pre("third")["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_permission_denial_stops_replay(self):
        await self.register()
        self.pre()
        await self.finish("call", {"permission_denied": True})
        self.assertTrue(self.store.denied("session", self.signature))
        retry = lambda *args: decisions.retry_decision(*args, evaluate=AsyncMock())
        result = await routing.select_action("session", "Run tests", [self.action], self.choose, retry, self.store)
        self.assertEqual(result["disposition"], "stop")

    async def test_raw_arguments_and_output_are_not_persisted(self):
        self.action = candidate("echo SECRET-LOCAL-ONLY")
        self.choose.return_value = {"disposition": "selected", "selected_candidate": self.action}
        await self.register()
        self.pre()
        await self.finish("call", {"exit_code": 0, "output": "SECRET-RESULT-ONLY"})
        data = self.store.path.read_bytes()
        self.assertNotIn(b"SECRET", data)

    async def test_unknown_and_explicit_outcomes(self):
        self.assertEqual(routing.outcome("All done"), "unknown")
        self.assertEqual(routing.outcome("Process exited with code 1\n"), "unknown")
        self.assertEqual(routing.outcome({"isError": True}), "failed")
        self.assertEqual(routing.outcome({"exit_code": 0}), "passed")
        self.assertEqual(routing.outcome("Automatic approval review rejected the action"), "unknown")


    async def test_fallback_consumes_all_sibling_candidates_atomically(self):
        self.choose.return_value = {"disposition": "main", "reason": "uncertain"}
        other = candidate("python other.py")
        other["id"] = "other"
        await routing.select_action("session", "Test", [self.action, other], self.choose, self.retry, self.store)
        signatures = [self.signature, routing.fingerprint("Bash", other["input"])]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda pair: self.store.reserve("session", *pair),
                                        zip(("one", "two"), signatures)))
        self.assertEqual(sorted(results), [False, True])

    async def test_fallback_without_eligible_candidates_explicitly_replans(self):
        await self.register()
        self.pre()
        await self.finish("call", {"exit_code": 1})
        self.choose.return_value = {"disposition": "main"}
        result = await self.register()
        self.assertEqual(result["next_step"], "replan")
        self.assertEqual(result["reason"], "no_eligible_candidates")
        self.assertEqual(self.pre("blocked")["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_nested_failures_and_denials_override_protocol_success(self):
        cases = [({"isError": False, "structuredContent": {"exit_code": 1}}, "failed"),
                 ({"exit_code": 0, "structured_content": {"permission_denied": True}}, "denied"),
                 ({"is_error": False, "content": [{"type": "text", "text": '{"status":"failed"}'}]}, "failed"),
                 ({"isError": False}, "unknown"), ({"is_error": False}, "unknown")]
        for value, expected in cases:
            self.assertEqual(routing.outcome(value), expected)

    async def test_protocol_success_does_not_clear_existing_failure(self):
        await self.register()
        self.pre()
        await self.finish("call", {"exit_code": 1})
        await self.register()
        self.pre("second")
        await self.finish("second", {"isError": False})
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_scopes_bind_grants_calls_and_failures(self):
        workspace = str(Path(self.temp.name).resolve())
        elsewhere = workspace + "/another"
        await routing.select_action("session", "Test", [self.action], self.choose, self.retry,
                                    self.store, workspace, "turn-a")
        for cwd, turn, session in [(elsewhere, "turn-a", "session"),
                                    (workspace, "turn-b", "session"),
                                    (workspace, "turn-a", "other-session")]:
            self.assertFalse(self.store.reserve(session, "call", self.signature, cwd, turn))
        self.assertTrue(self.store.reserve("session", "call", self.signature, workspace, "turn-a"))
        self.assertIsNone(self.store.complete("session", "call", self.signature, "failed", workspace, "turn-b"))
        self.store.complete("session", "call", self.signature, "failed", workspace, "turn-a")
        self.assertEqual(self.store.failures("session", self.signature, workspace), 1)
        self.assertEqual(self.store.failures("session", self.signature, elsewhere), 0)
        self.retry.reset_mock()
        await routing.select_action("session", "Test", [self.action], self.choose, self.retry,
                                    self.store, workspace, "turn-b")
        self.assertEqual(self.retry.call_args.args[2], 1)
        self.assertTrue(self.store.reserve("session", "call", self.signature, workspace, "turn-b"))

    async def test_denial_context_provides_exact_scope_arguments(self):
        cwd = str(Path(self.temp.name).resolve())
        result = routing.pre_tool("session", "call", "Bash", self.action["input"],
                                  self.store, cwd, "turn-123")["hookSpecificOutput"]
        text = result["additionalContext"]
        self.assertIn('"session_id": "session"', text)
        self.assertIn(json.dumps(cwd), text)
        self.assertIn('"turn_id": "turn-123"', text)
        self.assertNotIn(self.action["input"]["command"], text)

    async def test_full_observed_shell_options_must_match(self):
        self.action = candidate()
        self.action["input"]["workdir"] = "/workspace/one"
        self.action["input"]["timeout_ms"] = 1000
        self.choose.return_value = {"disposition": "selected", "selected_candidate": self.action}
        await self.register()
        for changes in ({"workdir": "/workspace/two"}, {"timeout_ms": 2000}):
            altered = {**self.action["input"], **changes}
            self.assertFalse(self.store.reserve("session", "wrong", routing.fingerprint("Bash", altered)))
        normalized = dict(self.action["input"])
        normalized["cmd"] = normalized.pop("command")
        self.assertTrue(self.store.reserve("session", "right", routing.fingerprint("exec_command", normalized)))

    async def test_failure_invalidates_grant_issued_while_call_pending(self):
        self.store.grant("session", [self.action])
        self.assertTrue(self.store.reserve("session", "first", self.signature))
        self.assertTrue(self.store.grant("session", [self.action]))
        self.store.complete("session", "first", self.signature, "denied")
        self.assertFalse(self.store.reserve("session", "second", self.signature))
        self.assertTrue(self.store.denied("session", self.signature))

    async def test_stale_success_and_unknown_cannot_erase_new_failure(self):
        for identifier in ("first", "second", "third"):
            self.store.grant("session", [self.action])
            self.assertTrue(self.store.reserve("session", identifier, self.signature))
        self.store.complete("session", "first", self.signature, "failed")
        self.store.complete("session", "second", self.signature, "passed")
        self.store.complete("session", "third", self.signature, "unknown")
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_changed_failure_revision_rejects_inflight_model_decision(self):
        self.store.grant("session", [self.action])
        self.store.reserve("session", "pending", self.signature)
        self.store.complete("session", "pending", self.signature, "failed")
        async def racing_retry(*args):
            self.store.grant("session", [self.action], expected_revisions={self.signature: 1})
            self.store.reserve("session", "other", self.signature)
            self.store.complete("session", "other", self.signature, "failed")
            return {"next_step": "retry"}
        result = await routing.select_action("session", "Test", [self.action], self.choose,
                                             racing_retry, self.store)
        self.assertEqual(result["reason"], "failure_state_changed")
        self.assertFalse(self.store.reserve("session", "late", self.signature))

    async def test_provider_cannot_override_deterministic_repeat_or_denial(self):
        for identifier in ("first", "second"):
            self.store.grant("session", [self.action], expected_revisions={self.signature: self.store.state("session", self.signature)[2]})
            self.store.reserve("session", identifier, self.signature)
            self.store.complete("session", identifier, self.signature, "failed")
        result = await self.register()
        self.assertEqual(result["disposition"], "architect")
        self.assertNotEqual(result["next_step"], "retry")
        self.retry.assert_not_called()

    async def test_unrecognized_selection_cannot_survive_main_fallback(self):
        self.choose.return_value = {"disposition": "selected", "selected_candidate": candidate("unknown")}
        result = await self.register()
        self.assertEqual(result["disposition"], "main")
        self.assertIsNone(result["selected_candidate"])

    async def test_expiry_cleanup_and_closed_database_connections(self):
        self.store.grant("session", [self.action])
        with self.store.connect() as db:
            db.execute("UPDATE route_groups_v2 SET expires=0")
        with self.assertRaises(Exception):
            db.execute("SELECT 1")
        self.assertFalse(self.store.reserve("session", "expired", self.signature))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM route_grants_v2").fetchone()[0], 0)
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


    def fail_twice(self):
        for index in range(2):
            revision = self.store.state("session", self.signature)[2]
            self.store.grant("session", [self.action], expected_revisions={self.signature: revision})
            self.store.reserve("session", f"failure-{index}", self.signature)
            self.store.complete("session", f"failure-{index}", self.signature, "failed")

    async def test_reviewed_retry_is_one_use_and_success_clears_failure_count(self):
        self.fail_twice()
        result = await routing.reviewed_retry("session", "", "", self.action,
                                              "architect-review-123", "Architect reviewed the source repair.", self.store)
        self.assertEqual(result["disposition"], "selected")
        self.assertEqual(self.store.failures("session", self.signature), 2)
        again = await routing.reviewed_retry("session", "", "", self.action,
                                             "architect-review-123", "Same review", self.store)
        self.assertEqual(again["reason"], "review_already_used")
        self.assertTrue(self.store.reserve("session", "reviewed-call", self.signature))
        self.assertFalse(self.store.reserve("session", "duplicate", self.signature))
        self.store.complete("session", "reviewed-call", self.signature, "passed")
        self.assertEqual(self.store.failures("session", self.signature), 0)
        self.assertEqual((await self.register())["disposition"], "selected")
        data = self.store.path.read_bytes()
        self.assertNotIn(b"architect-review-123", data)
        self.assertNotIn(b"source repair", data)

    async def test_reviewed_unknown_does_not_reset_count_or_reuse_attestation(self):
        self.fail_twice()
        await routing.reviewed_retry("session", "", "", self.action, "ref", "Reviewed repair", self.store)
        self.store.reserve("session", "unknown", self.signature)
        self.store.complete("session", "unknown", self.signature, "unknown")
        self.assertEqual(self.store.failures("session", self.signature), 2)
        again = await routing.reviewed_retry("session", "", "next-turn", self.action,
                                             "ref", "Reviewed repair", self.store)
        self.assertEqual(again["reason"], "report_outcome_required")
        self.store.reconcile("session", "unknown", "passed")
        again = await routing.reviewed_retry("session", "", "next-turn", self.action, "ref", "Reviewed repair", self.store)
        self.assertEqual(again["reason"], "review_not_required")

    async def test_reviewed_retry_requires_bounded_attestation_and_safe_candidate(self):
        self.fail_twice()
        cases = [(self.action, "", "Reviewed"), (self.action, "ref", ""),
                 (self.action, "x" * 201, "Reviewed"), (self.action, "ref", "x" * 1501),
                 (candidate(safe=False), "ref", "Reviewed"), ({"id": "bad"}, "ref", "Reviewed")]
        for action, reference, summary in cases:
            result = await routing.reviewed_retry("session", "", "", action, reference, summary, self.store)
            self.assertEqual(result["reason"], "invalid_review_attestation")
        self.assertFalse(self.store.reserve("session", "not-reviewed", self.signature))

    async def test_reviewed_retry_stops_after_single_denial(self):
        self.store.grant("session", [self.action])
        self.store.reserve("session", "denied", self.signature)
        self.store.complete("session", "denied", self.signature, "denied")
        result = await routing.reviewed_retry("session", "", "", self.action, "review", "Reviewed", self.store)
        self.assertEqual(result["next_step"], "stop")

    async def test_equivalent_candidates_do_not_duplicate_reservations(self):
        second = {**self.action, "id": "equivalent", "retry_safe": False}
        self.assertTrue(self.store.grant("session", [self.action, second]))
        self.assertTrue(self.store.reserve("session", "one", self.signature))
        self.assertFalse(self.store.reserve("session", "two", self.signature))
        record = self.store.complete("session", "one", self.signature, "failed")
        self.assertFalse(record["retry_safe"])

    async def test_reviewed_retry_never_overrides_denial(self):
        self.fail_twice()
        revision = self.store.state("session", self.signature)[2]
        self.store.grant("session", [self.action], expected_revisions={self.signature: revision})
        self.store.reserve("session", "denied", self.signature)
        self.store.complete("session", "denied", self.signature, "denied")
        result = await routing.reviewed_retry("session", "", "", self.action, "review", "Reviewed", self.store)
        self.assertEqual(result["next_step"], "stop")
        self.assertFalse(self.store.reserve("session", "cannot-override", self.signature))

    async def test_reviewed_retry_binds_workspace_turn_and_failure_revision(self):
        self.fail_twice()
        other_workspace = str(Path(self.temp.name).resolve() / "other-workspace")
        result = await routing.reviewed_retry("session", other_workspace, "", self.action,
                                              "review", "Reviewed", self.store)
        self.assertEqual(result["reason"], "review_not_required")
        await routing.reviewed_retry("session", "", "turn-a", self.action, "review-a", "Reviewed", self.store)
        self.assertFalse(self.store.reserve("session", "bad-turn", self.signature, "", "turn-b"))
        self.assertTrue(self.store.reserve("session", "first", self.signature, "", "turn-a"))
        await routing.reviewed_retry("session", "", "turn-a", self.action, "review-b", "Reviewed", self.store)
        self.store.complete("session", "first", self.signature, "failed", "", "turn-a")
        self.assertFalse(self.store.reserve("session", "stale-review", self.signature, "", "turn-a"))
        self.assertEqual(self.store.failures("session", self.signature), 3)

    async def test_disjoint_decisions_support_parallel_reservations(self):
        second = candidate("python other.py")
        second["id"] = "other"
        self.assertTrue(self.store.grant("session", [self.action]))
        self.assertTrue(self.store.grant("session", [second]))
        self.assertTrue(self.store.reserve("session", "first", self.signature))
        self.assertTrue(self.store.reserve("session", "second", routing.fingerprint("Bash", second["input"])))

    async def test_overlapping_registration_replaces_only_its_decision_group(self):
        second, third = candidate("python second.py"), candidate("python third.py")
        second["id"], third["id"] = "second", "third"
        self.store.grant("session", [self.action, second])
        self.store.grant("session", [third])
        self.store.grant("session", [self.action])
        self.assertFalse(self.store.reserve("session", "old-sibling", routing.fingerprint("Bash", second["input"])))
        self.assertTrue(self.store.reserve("session", "parallel", routing.fingerprint("Bash", third["input"])))
        self.assertTrue(self.store.reserve("session", "replacement", self.signature))

    async def test_stdout_permission_phrases_cannot_latch_denial(self):
        cases = [("Process exited with code 0\nOutput:\npermission denied fixture handled successfully\n", "unknown"),
                 ({"exit_code": 0, "output": "Automatic approval review rejected the action"}, "passed"),
                 ({"exit_code": 0, "content": [{"text": '{"permission_denied":true}'}]}, "passed"),
                 ("permission denied", "unknown"),
                 ({"permission_denied": True, "exit_code": 0}, "denied"),
                 ({"isError": False, "structuredContent": {"permission_denied": True}}, "denied")]
        for payload, expected in cases:
            self.assertEqual(routing.outcome(payload), expected)


    async def make_unknown(self, identifier="native-call", cwd="", turn_id=""):
        self.store.grant("session", [self.action], cwd, turn_id)
        self.assertTrue(self.store.reserve("session", identifier, self.signature, cwd, turn_id))
        return await routing.post_tool("session", identifier, "Bash", self.action["input"],
                                       "JEV_SHAPE_PROBE\n", self.store, self.retry, cwd, turn_id)

    async def test_real_native_stdout_shape_requires_attested_report(self):
        response = await self.make_unknown()
        text = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("report_outcome", text)
        self.assertIn('"tool_use_id": "native-call"', text)
        self.assertNotIn("permissionDecision", response["hookSpecificOutput"])
        result = await routing.report_outcome("session", "", "", "native-call", 0, False, self.store, self.retry)
        self.assertEqual(result["recorded_outcome"], "passed")
        self.assertEqual(result["outcome_attested_by"], "main")
        self.assertIsNone(self.store.unresolved("session", self.signature))
        self.assertEqual((await self.register())["disposition"], "selected")

    async def test_reported_failure_invokes_retry_once_and_cannot_be_overwritten(self):
        await self.make_unknown()
        self.retry.reset_mock()
        result = await routing.report_outcome("session", "", "", "native-call", 1, False, self.store, self.retry)
        self.assertEqual(result["recorded_outcome"], "failed")
        self.assertEqual(self.store.failures("session", self.signature), 1)
        self.retry.assert_awaited_once()
        for code in (1, 0):
            duplicate = await routing.report_outcome("session", "", "", "native-call", code, False, self.store, self.retry)
            self.assertEqual(duplicate["reason"], "no_unresolved_matching_call")
        self.assertEqual(self.store.failures("session", self.signature), 1)
        self.retry.assert_awaited_once()

    async def test_unknown_cannot_be_retried_even_across_turns(self):
        await self.make_unknown(turn_id="prior-turn")
        result = await routing.select_action("session", "Tests", [self.action], self.choose,
                                             self.retry, self.store, "", "new-turn")
        self.assertEqual(result["next_step"], "report_outcome")
        self.assertEqual(result["report_arguments"]["turn_id"], "prior-turn")
        self.assertEqual(result["report_arguments"]["tool_use_id"], "native-call")
        self.assertFalse(self.store.grant("session", [self.action], "", "new-turn"))

    async def test_unknown_blocks_grant_issued_before_unknown_completion(self):
        self.store.grant("session", [self.action])
        self.store.reserve("session", "first", self.signature)
        self.store.grant("session", [self.action])
        self.store.complete("session", "first", self.signature, "unknown")
        self.assertFalse(self.store.reserve("session", "cannot-replay", self.signature))

    async def test_concurrent_reports_record_once(self):
        await self.make_unknown()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.store.reconcile("session", "native-call", "failed"), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_stale_report_success_cannot_clear_concurrent_failure(self):
        self.store.grant("session", [self.action])
        self.store.reserve("session", "unknown", self.signature)
        self.store.grant("session", [self.action])
        self.store.reserve("session", "failed", self.signature)
        self.store.complete("session", "unknown", self.signature, "unknown")
        self.store.complete("session", "failed", self.signature, "failed")
        result = await routing.report_outcome("session", "", "", "unknown", 0, False, self.store, self.retry)
        self.assertEqual(result["recorded_outcome"], "passed")
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_missing_invalid_and_explicit_call_reports_change_nothing(self):
        missing = await routing.report_outcome("session", "", "", "missing", 1, False, self.store, self.retry)
        self.assertEqual(missing["reason"], "no_unresolved_matching_call")
        for code, denied in [(None, False), (True, False), ("1", False), (0, "false")]:
            result = await routing.report_outcome("session", "", "", "missing", code, denied, self.store, self.retry)
            self.assertEqual(result["reason"], "invalid_outcome_attestation")
        await self.register()
        self.pre("explicit")
        await self.finish("explicit", {"exit_code": 1})
        result = await routing.report_outcome("session", "", "", "explicit", 0, False, self.store, self.retry)
        self.assertEqual(result["reason"], "no_unresolved_matching_call")
        self.assertEqual(self.store.failures("session", self.signature), 1)

    async def test_reported_permission_denial_stops_without_provider_override(self):
        await self.make_unknown()
        self.retry.reset_mock()
        result = await routing.report_outcome("session", "", "", "native-call", None, True, self.store, self.retry)
        self.assertEqual(result["next_step"], "stop")
        self.assertTrue(self.store.denied("session", self.signature))
        self.retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
