"""Offline role-only boundary tests; never call the provider."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp import Client
import role_server as server


def response(choice="worker", confidence=0.95, roles=("worker",)):
    probabilities = {role: 0.0 for role in ("main", *roles)}
    probabilities[choice] = 1.0
    return SimpleNamespace(choices={"decision": SimpleNamespace(
        choice=choice, confidence=confidence, probabilities=probabilities)}, model=server.MODEL)


class RoleBoundaries(unittest.IsolatedAsyncioTestCase):
    def assert_fallback(self, result):
        self.assertEqual(result["disposition"], "main")
        self.assertFalse(result["validated"])
        for key in ("agent_type", "selected_role", "decision", "model", "reasoning_effort",
                    "spawn", "permission", "grants_action"):
            self.assertNotIn(key, result)

    async def test_valid_selection_and_supplied_candidates(self):
        with patch.object(server, "_request", AsyncMock(return_value=response())) as request:
            result = await server.route_work("Implement one bounded change", ["worker"])
        self.assertEqual(result["status"], "advisory")
        self.assertEqual(result["disposition"], "selected")
        self.assertTrue(result["validated"])
        self.assertEqual(result["agent_type"], "worker")
        self.assertEqual(result["selected_role"], "worker")
        self.assertEqual(result["advisor_model"], "jev-1.14.0")
        self.assertTrue(result["threshold_is_provisional"])
        self.assertEqual(set(request.call_args.args[1]), {"worker", "main"})
        self.assertNotIn("model", result)
        self.assertNotIn("reasoning_effort", result)

    async def test_excluded_unknown_and_main_choices(self):
        for choice in ("architect", "invented", "main"):
            with self.subTest(choice=choice), patch.object(
                server, "_request", AsyncMock(return_value=response(choice=choice))
            ):
                result = await server.route_work("A task", ["worker"])
            self.assert_fallback(result)

    async def test_threshold(self):
        for confidence, selected in ((0.69, False), (0.70, True)):
            with patch.object(server, "_request", AsyncMock(return_value=response(confidence=confidence))):
                result = await server.route_work("A task", ["worker"])
            self.assertEqual(result["validated"], selected)
            if not selected:
                self.assert_fallback(result)
                self.assertEqual(result["reason"], "uncertain")

    async def test_each_available_role_is_eligible(self):
        roles = ["explorer", "researcher", "worker", "architect"]
        for role in roles:
            with self.subTest(role=role), patch.object(
                server, "_request", AsyncMock(return_value=response(choice=role, roles=roles))
            ):
                result = await server.route_work("A bounded task", roles)
            self.assertTrue(result["validated"])
            self.assertEqual(result["agent_type"], role)

    async def test_malformed_distribution_and_numbers(self):
        malformed = []
        for confidence in (float("nan"), float("inf"), True, -0.1, 1.1, "0.9", None):
            malformed.append(response(confidence=confidence))
        for probabilities in ({"worker": 1.0}, {"main": 0, "worker": 1, "architect": 0},
                              {"main": 0.1, "worker": 1}, {"main": 0.8, "worker": 0.2},
                              {"main": 0, "worker": float("nan")},
                              {"main": 0, "worker": True},
                              {"main": -0.1, "worker": 1.1}, None, []):
            value = response()
            value.choices["decision"].probabilities = probabilities
            malformed.append(value)
        malformed.extend((None, SimpleNamespace(choices={}), SimpleNamespace(choices=None)))
        for value in malformed:
            with self.subTest(value=value), patch.object(server, "_request", AsyncMock(return_value=value)):
                result = await server.route_work("A task", ["worker"])
            self.assert_fallback(result)
            self.assertEqual(result["reason"], "invalid_response")

    async def test_bad_input_does_not_request(self):
        cases = [("", ["worker"]), ("  ", ["worker"]), (None, ["worker"]),
                 ("x" * (server.MAX_INPUT + 1), ["worker"]),
                 ("x" * server.MAX_INPUT, ["worker"]), ("bad\x00summary", ["worker"]),
                 ("bad\x7fsummary", ["worker"]), ("bad\ud800summary", ["worker"]),
                 ("A task", []), ("A task", ["main"]), ("A task", ["unknown"]),
                 ("A task", ["worker", "worker"]), ("A task", "worker"),
                 ("A task", [True]), ("A task", [{}]), ("A task", None)]
        with patch.object(server, "_request", AsyncMock()) as request:
            for summary, roles in cases:
                with self.subTest(summary=repr(summary)[:50], roles=roles):
                    self.assert_fallback(await server.route_work(summary, roles))
            request.assert_not_called()

    async def test_timeout_and_exception_do_not_leak(self):
        async def slow(*args):
            await asyncio.sleep(10)
        with patch.object(server, "_request", slow), patch.object(server, "TIMEOUT", 0.01):
            result = await server.route_work("A task", ["worker"])
        self.assert_fallback(result)
        self.assertEqual(result["reason"], "timeout")
        with patch.object(server, "_request", AsyncMock(side_effect=RuntimeError("SECRET PROVIDER BODY"))):
            result = await server.route_work("A task", ["worker"])
        self.assert_fallback(result)
        self.assertNotIn("SECRET", str(result))

    async def test_missing_key_never_initializes_client(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch.object(server, "AsyncTypeSafeClient") as client:
            self.assert_fallback(await server.route_work("A task", ["worker"]))
        client.assert_not_called()

    async def test_sdk_request_contract_offline(self):
        fake = AsyncMock()
        fake.system_one.return_value = response()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "offline-fake"}), patch.object(
            server, "AsyncTypeSafeClient"
        ) as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=fake)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await server.route_work("Implement one function", ["worker"])
        self.assertTrue(result["validated"])
        self.assertEqual(factory.call_args.kwargs["model"], "jev-1.14.0")
        self.assertEqual(factory.call_args.kwargs["timeout"], 15)
        self.assertEqual(factory.call_args.kwargs["retry"].max_retries, 0)
        call = fake.system_one.call_args.kwargs
        self.assertEqual(call["state"], {"task_summary": "Implement one function", "available_roles": ["worker"]})
        self.assertEqual(set(call["questions"]), {"decision"})
        self.assertEqual(set(call["questions"]["decision"].criteria), {"main", "worker"})

    async def test_exact_mcp_registry_and_structured_result(self):
        with patch.object(server, "_request", AsyncMock(return_value=response())):
            async with Client(server.mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual([tool.name for tool in tools], ["route_work"])
                schema = tools[0].input_schema
                self.assertEqual(set(schema["properties"]), {"task_summary", "available_roles"})
                result = await client.call_tool("route_work", {
                    "task_summary": "Implement one change", "available_roles": ["worker"]})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["agent_type"], "worker")

    def test_import_does_not_load_routing_or_create_state(self):
        # A clean interpreter avoids hiding imports behind other test modules.
        script = """
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'server', 'routing', 'decisions', 'sqlite3'}:
            raise AssertionError('Forbidden import: ' + fullname)
sys.meta_path.insert(0, Block())
import role_server
assert not hasattr(role_server, 'store')
assert set(sys.modules).isdisjoint({'server', 'routing', 'decisions'})
assert role_server.logging.getLogger('typesafe_sdk').disabled
"""
        result = subprocess.run([sys.executable, "-c", script],
                                cwd=Path(server.__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
