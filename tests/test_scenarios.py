"""Process durability and closed adapter boundaries, using actual SQLite workers."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from roomrill.scenarios import Controller, domain, receipts, run

EPOCH = "2030-01-01T12:00:00Z"
BODY = {
    "id": "A",
    "room": "meeting-a",
    "targets": {"light": {"on": True, "brightness": 60}},
    "priority": 10,
    "duration_seconds": 30,
    "restore": "compare",
}
OPERATOR = {"owner": "test-operator", "role": "operator"}


class ScenarioProcesses(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.controller = Controller(self.output / "case.sqlite", EPOCH, self.output)
        response = self.controller.call("seed", epoch=EPOCH)
        self.assertTrue(response["ok"])

    def tearDown(self):
        self.controller.close()
        self.temporary.cleanup()

    def test_restart_preserves_receipt_and_duplicate_cannot_apply_twice(self):
        accepted = self.controller.call("submit", body=BODY, operator_context=OPERATOR)
        self.assertTrue(accepted["ok"])
        applied = self.controller.call("settle")
        before = applied["raw"]
        self.assertEqual(len(receipts(before)), 1)
        command_id = next(iter(receipts(before)))
        old_pid = self.controller.process.pid
        self.assertTrue(self.controller.restart(EPOCH))
        self.assertNotEqual(old_pid, self.controller.process.pid)
        reopened = self.controller.call("inspect")
        self.assertEqual(domain(before), domain(reopened["raw"]))
        replay = self.controller.call("deliver", command_id=command_id)
        self.assertTrue(replay["ok"])
        self.assertEqual(receipts(before)[command_id], replay["result"])
        self.assertEqual(receipts(before), receipts(replay["raw"]))
        self.assertEqual(before["rooms"], replay["raw"]["rooms"])
        self.assertEqual(self.controller.processes[0]["exit_code"], 0)

    def test_unknown_payload_authority_and_worker_fields_are_refused_without_mutation(self):
        before = self.controller.call("inspect")["raw"]
        for field, value in (("owner", "invented"), ("role", "operator"), ("unexpected", True)):
            body = {**copy.deepcopy(BODY), field: value}
            response = self.controller.call("submit", body=body, operator_context=OPERATOR)
            self.assertFalse(response["ok"], field)
            self.assertEqual(response["error"]["code"], "invalid_input")
            self.assertEqual(domain(before), domain(response["raw"]))
        unknown = self.controller.call("evaluate_python", expression="1+1")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "adapter_input")
        unknown = self.controller.call("inspect", ignored_field=True)
        self.assertFalse(unknown["ok"])
        self.assertEqual(domain(before), domain(unknown["raw"]))


class ScenarioInstructions(unittest.TestCase):
    def test_unknown_assertion_fails_and_output_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            corpus = {
                "schema_version": "roomrill.authored-acceptance.v1",
                "set_kind": "fixed_authored_synthetic_acceptance",
                "fixture_epoch": EPOCH,
                "default_initial": {"device": {}, "observed": {}},
                "cases": [
                    {
                        "id": "T01",
                        "title": "Unknown assertion",
                        "steps": [
                            {
                                "id": "inspect",
                                "action": "inspect",
                                "at": EPOCH,
                                "expect": {"unimplemented_property": True},
                            }
                        ],
                    }
                ],
            }
            source = base / "cases.json"
            source.write_text(json.dumps(corpus), encoding="utf-8")
            result = run(source, base / "evidence")
            self.assertFalse(result["passed"])
            self.assertEqual(result["failed_cases"], ["T01"])
            step = json.loads((base / "evidence/T01/step-01-inspect.json").read_text())
            self.assertIn("Unknown or invalid expectation", step["adapter_error"]["message"])
            original = (base / "evidence/summary.json").read_bytes()
            with self.assertRaises(FileExistsError):
                run(source, base / "evidence")
            self.assertEqual(original, (base / "evidence/summary.json").read_bytes())

    def test_public_cases_preserve_the_original_sealed_bytes(self):
        path = Path(__file__).resolve().parents[1] / "examples/scenarios-v1.json"
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "07697fadfac2669401b58f90345603c48e270221df73c847ebc038a93ac07b23",
        )
        corpus = json.loads(path.read_bytes())
        self.assertEqual(len(corpus["cases"]), 41)
        self.assertEqual(sum(len(case["steps"]) for case in corpus["cases"]), 233)


if __name__ == "__main__":
    unittest.main()
