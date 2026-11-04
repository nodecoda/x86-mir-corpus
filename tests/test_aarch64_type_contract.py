"""AArch64 type contracts in the sidecar MIR driver.

The A2 witness triage found 552 asserts caused by type-slot combinations that
no IR producer can form (scalable vectors on fixed-width paths, role-swapped
conversions, unsupported ABDS vector shapes).  The contract must turn those
into ``construction_mismatch`` while keeping reviewed valid controls alive.
"""

import json
import os
import subprocess
import tempfile
import unittest

import testlib

RUN = testlib.sidecar_binary("x86-mir-run")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "aarch64-type-contract.json")


class AArch64TypeContractTest(unittest.TestCase):
    def run_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.jsonl")
            env = dict(os.environ)
            env.update({
                "X86_LEGALIZER_MIR_CORPUS_IN": FIXTURE,
                "X86_LEGALIZER_MIR_OUT": out,
                "X86_LEGALIZER_MIR_MAX_STEPS": "10000",
                "X86_LEGALIZER_MIR_SHARD": "0/1",
                "X86_LEGALIZER_MIR_RULES": "production",
                "X86_LEGALIZER_MIR_START_INDEX": "0",
            })
            proc = subprocess.run([RUN], env=env, capture_output=True,
                                  text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle]
        return {e["id"]: e for e in events if e.get("event") == "outcome"}

    def expectations(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            return json.load(handle)["expectations"]

    def test_fixture_covers_each_new_family_and_controls(self):
        kinds = list(self.expectations().values())
        self.assertGreaterEqual(kinds.count("type_contract"), 10)
        self.assertGreaterEqual(kinds.count("legal"), 6)

    def test_invalid_slots_are_construction_mismatch(self):
        if RUN is None:
            self.skipTest("x86-mir-run is not built")
        outcomes = self.run_fixture()
        for atom_id, kind in self.expectations().items():
            self.assertIn(atom_id, outcomes, "no outcome for " + atom_id)
            outcome = outcomes[atom_id]
            if kind == "type_contract":
                self.assertEqual(outcome["outcome"], "construction_mismatch",
                                 atom_id)
                self.assertEqual(outcome["detail_code"], "type_contract",
                                 atom_id)
                self.assertEqual(outcome["steps"], 0, atom_id)
                self.assertIn("type contract:", outcome["detail"], atom_id)
            else:
                self.assertNotEqual(outcome["outcome"],
                                    "construction_mismatch", atom_id)
                self.assertNotEqual(outcome["detail_code"], "type_contract",
                                    atom_id)

    def test_driver_does_not_abort_on_the_fixture(self):
        if RUN is None:
            self.skipTest("x86-mir-run is not built")
        outcomes = self.run_fixture()
        self.assertEqual(len(outcomes), len(self.expectations()))


if __name__ == "__main__":
    unittest.main()
