"""Offline checks for candidate boundaries and deterministic retry controls."""

import json
import unittest
from unittest.mock import AsyncMock

import decisions


def evaluator(choice="candidate", confidence=0.95, status="advisory"):
    async def evaluate(state, questions, options):
        return {"status": status, "decision": {
            "choice": choice, "confidence": confidence,
            "probabilities": {key: float(key == choice) for key in options}}}
    return AsyncMock(side_effect=evaluate)


def file_candidate(**changes):
    return {"id": "candidate", "path": "src/app.py", "summary": "Application entry", **changes}


def action_candidate(**changes):
    return {"id": "candidate", "tool_name": "exec_command", "files": ["src/app.py"],
            "summary": "Run the already prepared local check", "retry_safe": True,
            "input": {"cmd": "SECRET-LOCAL-COMMAND", "env": {"KEY": "secret-token"}}, **changes}


class Decisions(unittest.IsolatedAsyncioTestCase):
    async def test_choices_select_only_supplied_objects(self):
        cases = [(decisions.choose_file, file_candidate()),
                 (decisions.choose_tool, {"id": "candidate", "name": "read_file", "summary": "Read"}),
                 (decisions.choose_action, action_candidate())]
        for choose, candidate in cases:
            with self.subTest(choose=choose.__name__):
                evaluate = evaluator()
                result = await choose("Inspect the application", [candidate], evaluate)
                self.assertEqual(result["disposition"], "selected")
                self.assertIs(result["selected_candidate"], candidate)
                self.assertEqual(set(evaluate.call_args.args[1]), {"decision"})

    async def test_action_input_and_unknown_fields_never_leave_process(self):
        candidate = action_candidate(extra_secret="extra-local-secret")
        evaluate = evaluator()
        result = await decisions.choose_action("Run validation", [candidate], evaluate)
        outbound = json.dumps(evaluate.call_args.args[0])
        for secret in ("SECRET-LOCAL-COMMAND", "secret-token", "extra-local-secret", '"input"', '"env"'):
            self.assertNotIn(secret, outbound)
        self.assertIs(result["selected_candidate"]["input"], candidate["input"])

    async def test_invalid_candidate_inputs_never_call_provider(self):
        bad_lists = [None, [], [None], [file_candidate()] * 2,
                     [file_candidate(id="main")], [file_candidate(id="bad id")],
                     [file_candidate(id=12)], [file_candidate(summary="x" * 501)],
                     [file_candidate(summary="")], [file_candidate(path="../secret")],
                     [file_candidate(path="/tmp/app.py")], [file_candidate(path="C:\\app.py")],
                     [file_candidate(path="src/.env.local")], [file_candidate(path=".ssh/id_rsa")],
                     [file_candidate(path="credentials.json")], [file_candidate(path=".netrc")],
                     [file_candidate(path="keys/private_key.txt")], [file_candidate(path="keys/server.pem")],
                     [file_candidate(id=f"c{i}") for i in range(33)]]
        for candidates in bad_lists:
            with self.subTest(candidates=candidates):
                evaluate = evaluator()
                result = await decisions.choose_file("Read", candidates, evaluate)
                self.assertEqual(result["disposition"], "main")
                self.assertNotIn("selected_candidate", result)
                evaluate.assert_not_called()

    async def test_invalid_tool_and_action_metadata(self):
        cases = [(decisions.choose_tool, {"id": "candidate", "name": "", "summary": "Read"}),
                 (decisions.choose_action, action_candidate(input="not a dict")),
                 (decisions.choose_action, action_candidate(files=["../escape"])),
                 (decisions.choose_action, action_candidate(retry_safe=1))]
        for choose, candidate in cases:
            evaluate = evaluator()
            result = await choose("Task", [candidate], evaluate)
            self.assertEqual(result["disposition"], "main")
            evaluate.assert_not_called()

    async def test_input_lengths_and_total_payload_are_bounded(self):
        for task in (None, "", "x" * 1501):
            evaluate = evaluator()
            result = await decisions.choose_file(task, [file_candidate()], evaluate)
            self.assertEqual(result["disposition"], "main")
            evaluate.assert_not_called()
        evaluate = evaluator()
        candidates = [file_candidate(id=f"c{i}", summary="x" * 500) for i in range(32)]
        result = await decisions.choose_file("Task", candidates, evaluate)
        self.assertEqual(result["disposition"], "main")
        evaluate.assert_not_called()

    async def test_uncertainty_unavailability_and_unknown_choice_have_no_action(self):
        cases = [evaluator(confidence=0.69), evaluator(status="uncertain"),
                 evaluator(status="unavailable"), evaluator(choice="unknown"),
                 evaluator(confidence=float("nan")), evaluator(confidence=True)]
        for evaluate in cases:
            result = await decisions.choose_action("Task", [action_candidate()], evaluate)
            self.assertEqual(result["disposition"], "main")
            self.assertNotIn("selected_candidate", result)

    async def test_no_match_returns_to_main(self):
        result = await decisions.choose_file("Task", [file_candidate()], evaluator("main"))
        self.assertEqual(result["reason"], "no_matching_candidate")
        self.assertNotIn("selected_candidate", result)

    async def test_invalid_results_and_provider_errors_are_sanitized(self):
        responses = [None, {}, {"status": "advisory", "decision": "secret"},
                     {"status": "advisory", "decision": {"choice": "candidate", "confidence": 1,
                                                         "probabilities": {"candidate": 1}}}]
        for response in responses:
            result = await decisions.choose_file("Task", [file_candidate()],
                                                  AsyncMock(return_value=response))
            self.assertEqual(result["disposition"], "main")
            self.assertNotIn("secret", str(result))
        result = await decisions.choose_file("Task", [file_candidate()],
                                              AsyncMock(side_effect=ValueError("private-exception")))
        self.assertEqual(result["disposition"], "main")
        self.assertNotIn("private-exception", str(result))

    async def test_retry_deterministic_safety_guards_precede_provider(self):
        for kwargs, disposition in [({"permission_denied": True}, "stop"),
                                    ({"retry_safe": False}, "main"),
                                    ({"attempts": 2}, "architect"),
                                    ({"attempts": 30}, "architect"),
                                    ({"attempts": -1}, "main"),
                                    ({"attempts": True}, "main")]:
            args = dict(task_summary="Task", error_summary="Failure", attempts=1,
                        retry_safe=True, permission_denied=False)
            args.update(kwargs)
            evaluate = evaluator("retry")
            result = await decisions.retry_decision(**args, evaluate=evaluate)
            self.assertEqual(result["disposition"], disposition)
            self.assertNotEqual(result["next_step"], "retry")
            evaluate.assert_not_called()

    async def test_safe_retry_requires_confident_valid_choice(self):
        for choice in ("retry", "change", "stop", "main"):
            result = await decisions.retry_decision("Task", "Transient failure", 1, True, False,
                                                    evaluator(choice))
            self.assertEqual(result["next_step"], choice)
            self.assertEqual(result["disposition"], "stop" if choice == "stop" else "main")
        for evaluate in (evaluator("retry", 0.1), evaluator("retry", status="uncertain"),
                         evaluator("invented")):
            result = await decisions.retry_decision("Task", "Transient failure", 1, True, False,
                                                    evaluate)
            self.assertEqual(result["next_step"], "main")
        evaluate = evaluator("retry")
        result = await decisions.retry_decision("Task", "x" * 501, 1, True, False, evaluate)
        self.assertEqual(result["disposition"], "main")
        evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
