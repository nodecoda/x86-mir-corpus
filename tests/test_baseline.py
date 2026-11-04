"""Unit tests for baseline projection and checks."""

import json
import os
import tempfile
import unittest

from x86_mir_corpus import baseline, schemas


def atom(atom_id, outcome="legalized", steps=1, detail="ok",
         failed_on=None):
    return {"id": atom_id, "outcome": outcome, "steps": steps,
            "detail_code": detail, "failed_on_opcode": failed_on,
            "stderr_tail": "noise"}


def report(atoms):
    return {
        "schema": schemas.MIR_RUN_SCHEMA,
        "version": schemas.MIR_RUN_VERSION,
        "config": "C2",
        "corpus": {"atoms": len(atoms), "sha256": "abc", "config": "C2"},
        "counts": {"legalized": len(atoms)},
        "atoms": atoms,
    }


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, doc):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def test_projection_is_sorted_and_drops_volatile_fields(self):
        doc = baseline.project(report([atom("b"), atom("a")]))
        self.assertEqual([a["id"] for a in doc["atoms"]], ["a", "b"])
        self.assertNotIn("stderr_tail", doc["atoms"][0])
        self.assertEqual(doc["schema"],
                         schemas.BASELINE_PROJECTION_SCHEMA)

    def test_capture_then_check_is_clean(self):
        source = self.write("report.json", report([atom("a"), atom("b")]))
        out = os.path.join(self.tmp.name, "baseline.json")
        baseline.capture(source, out)
        check = baseline.check(source, out)
        self.assertTrue(check["ok"])
        self.assertEqual(check["added"], [])
        self.assertEqual(check["removed"], [])
        self.assertFalse(check["counts_changed"])

    def test_changed_outcome_lists_added_and_removed(self):
        source = self.write("report.json", report([atom("a"), atom("b")]))
        out = os.path.join(self.tmp.name, "baseline.json")
        baseline.capture(source, out)
        changed = self.write("changed.json", report([
            atom("a"), atom("b", outcome="failed", detail="boom")]))
        check = baseline.check(changed, out)
        self.assertFalse(check["ok"])
        self.assertEqual([a["id"] for a in check["added"]], ["b"])
        self.assertEqual([a["id"] for a in check["removed"]], ["b"])
        self.assertEqual(check["changed"][0]["id"], "b")
        self.assertEqual(check["changed"][0]["after"]["outcome"], "failed")

    def test_missing_atom_is_removed(self):
        source = self.write("report.json", report([atom("a"), atom("b")]))
        out = os.path.join(self.tmp.name, "baseline.json")
        baseline.capture(source, out)
        smaller = self.write("smaller.json", report([atom("a")]))
        check = baseline.check(smaller, out)
        self.assertEqual([a["id"] for a in check["removed"]], ["b"])
        self.assertEqual(check["added"], [])

    def test_rejects_a_raw_report_as_a_baseline(self):
        source = self.write("report.json", report([atom("a")]))
        raw = self.write("raw.json", report([atom("a")]))
        with self.assertRaises(SystemExit) as caught:
            baseline.check(source, raw)
        self.assertIn("baseline capture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
