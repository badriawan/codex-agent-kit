"""Offline boundary checks; no API calls or real credentials required."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp import Client

import server


def response(choice="main", confidence=0.95, options=None):
    options = options or server.ROLES
    probabilities = {key: 0.0 for key in options}
    probabilities[choice] = 1.0
    return SimpleNamespace(
        choices={"decision": SimpleNamespace(
            choice=choice, confidence=confidence, probabilities=probabilities)},
        scores={"risk": SimpleNamespace(score=0.1, confidence=0.9)},
        nouls={"needs_review": SimpleNamespace(noul=0.1)},
        model=server.MODEL,
    )


class Boundaries(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_exposes_structured_output(self):
        with patch.object(server, "_request", AsyncMock(return_value=response())):
            async with Client(server.mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, {
                    "route_work", "review_change", "choose_file", "choose_tool",
                    "retry_decision", "choose_action", "pre_tool", "post_tool",
                    "reviewed_retry", "report_outcome"})
                result = await client.call_tool("route_work", {"task_summary": "Read code"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["decision"]["choice"], "main")

    async def test_ambiguous_distribution_is_uncertain(self):
        r = response(confidence=0.2)
        r.choices["decision"].probabilities.update(main=0.51, worker=0.49)
        with patch.object(server, "_request", AsyncMock(return_value=r)):
            result = await server.route_work("A mixed task")
        self.assertEqual(result["status"], "uncertain")

    async def test_invalid_choice_and_nonfinite_values_are_rejected(self):
        for value in (response(choice="invented"), response(confidence=float("nan"))):
            with patch.object(server, "_request", AsyncMock(return_value=value)):
                result = await server.route_work("Read code")
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("decision", result)

    async def test_invalid_distribution_is_rejected(self):
        r = response()
        r.choices["decision"].probabilities["worker"] = 0.8
        with patch.object(server, "_request", AsyncMock(return_value=r)):
            result = await server.route_work("Read code")
        self.assertEqual(result["status"], "unavailable")

    async def test_request_has_a_wall_clock_timeout(self):
        async def slow(*args):
            await asyncio.sleep(10)
        with patch.object(server, "_request", slow), patch.object(server, "TIMEOUT", 0.01):
            result = await server.route_work("Read code")
        self.assertEqual(result["reason"], "timeout")

    async def test_api_error_does_not_echo_secrets(self):
        secret = "sensitive-provider-error-body"
        with patch.object(server, "_request", AsyncMock(side_effect=RuntimeError(secret))):
            result = await server.route_work("Read code")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn(secret, str(result))

    async def test_oversized_input_and_missing_key_make_no_client(self):
        with patch.object(server, "AsyncTypeSafeClient") as client:
            result = await server.route_work("x" * (server.MAX_INPUT + 1))
            self.assertEqual(result["status"], "unavailable")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                result = await server.route_work("Read code")
            self.assertEqual(result["status"], "unavailable")
            client.assert_not_called()

    async def test_known_facts_override_positive_assessment(self):
        cases = [
            ({"irreversible": True}, "architect_review"),
            ({"outside_plan": ["unplanned.py"]}, "architect_review"),
            ({"validation": "failed"}, "resolve_failed_validation"),
            ({"validation": "not_run"}, "collect_validation_evidence"),
        ]
        for changes, expected in cases:
            args = dict(plan="Edit a comment", change_summary="Comment updated", validation="passed")
            args.update(changes)
            with patch.object(server, "_request", AsyncMock(return_value=response(
                choice="continue", options=server.ASSESSMENTS))):
                result = await server.review_change(**args)
            self.assertEqual(result["required_next_step"], expected)
            self.assertTrue(result["advisory_only"])

    async def test_uncertain_risk_remains_uncertain(self):
        r = response(choice="continue", options=server.ASSESSMENTS)
        r.scores["risk"].confidence = 0.1
        with patch.object(server, "_request", AsyncMock(return_value=r)):
            result = await server.review_change("Edit a comment", "Comment updated", "passed")
        self.assertEqual(result["status"], "uncertain")

    async def test_known_facts_survive_api_failure(self):
        cases = [
            ({"validation": "failed"}, "resolve_failed_validation"),
            ({"validation": "passed", "outside_plan": ["unplanned.py"]}, "architect_review"),
        ]
        for arguments, expected in cases:
            with patch.object(server, "_request", AsyncMock(side_effect=RuntimeError("offline"))):
                result = await server.review_change("Edit comment", "Comment updated", **arguments)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["required_next_step"], expected)


if __name__ == "__main__":
    unittest.main()
