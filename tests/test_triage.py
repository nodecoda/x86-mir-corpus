"""Unit tests for triage clustering (synthetic, no LLVM required)."""

import json
import os
import tempfile
import unittest

from x86_mir_corpus import triage


def corpus_atom(atom_id, opcode, types, expected):
    return {"id": atom_id, "opcode": opcode, "rule_set": "X86_TEST",
            "query": {"types": types}, "expected": {"action": expected}}


def report_atom(atom_id, opcode, outcome, stderr="", steps=0):
    atom = {"id": atom_id, "opcode": opcode, "outcome": outcome,
            "steps": steps, "detail_code": "x"}
    if stderr:
        atom["stderr_tail"] = stderr
    return atom


ASSERT = ("X86Tests: /src/llvm/lib/CodeGen/GlobalISel/MachineIRBuilder.cpp:"
          "218: void llvm::MachineIRBuilder::buildInstr(): "
          "Assertion `isPreISelGenericOpcode(Opcode)' failed.")


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.corpus = {"atoms": [
            corpus_atom("a", "G_ADD", ["s32", "s32"], "Legal"),
            corpus_atom("b", "G_ADD", ["s64", "s64"], "Legal"),
            corpus_atom("c", "G_MUL", ["v2s32", "v2s32"], "Lower"),
            corpus_atom("d", "G_MUL", ["v4s32", "v4s32"], "Lower"),
        ]}
        self.report = {"corpus": {"atoms": 4, "config": "C2"}, "atoms": [
            report_atom("a", "G_ADD", "crash", ASSERT),
            report_atom("b", "G_ADD", "crash", ASSERT),
            report_atom("c", "G_MUL", "budget", steps=10001),
            report_atom("d", "G_MUL", "legalized", steps=3),
        ]}

    def test_clusters_crashes_by_assert_site_and_budgets_by_shape(self):
        payload = triage.cluster_report(
            self.report, self.corpus, "/corpus.json",
            driver="/build/x86-mir-run")
        self.assertEqual(payload["crash_cluster_count"], 1)
        self.assertEqual(payload["budget_cluster_count"], 1)
        crash = payload["crash_clusters"][0]
        self.assertEqual(crash["count"], 2)
        self.assertEqual(crash["assert_site"],
                         "MachineIRBuilder.cpp:218")
        self.assertEqual(crash["representatives"][0]["id"], "a")
        budget = payload["budget_clusters"][0]
        self.assertEqual(budget["opcode"], "G_MUL")
        self.assertEqual(budget["type_shape"], ["v2s32", "v2s32"])
        self.assertIn("x86_mir_corpus", budget["representatives"][0]["repro"])

    def test_cluster_output_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = os.path.join(tmp, "report.json")
            corpus_path = os.path.join(tmp, "corpus.json")
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(self.report, handle)
            with open(corpus_path, "w", encoding="utf-8") as handle:
                json.dump(self.corpus, handle)
            first = os.path.join(tmp, "first.json")
            second = os.path.join(tmp, "second.json")
            triage.cluster(report_path, corpus_path, first,
                           driver="/build/x86-mir-run")
            triage.cluster(report_path, corpus_path, second,
                           driver="/build/x86-mir-run")
            with open(first, "rb") as handle:
                first_bytes = handle.read()
            with open(second, "rb") as handle:
                second_bytes = handle.read()
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first_bytes.endswith(b"\n"))

    def test_markdown_renders_both_tables(self):
        payload = triage.cluster_report(
            self.report, self.corpus, "/corpus.json")
        text = triage.render_markdown(payload)
        self.assertIn("## Crash clusters", text)
        self.assertIn("## Budget clusters", text)
        self.assertIn("MachineIRBuilder.cpp:218", text)

    def test_repro_command_quotes_paths(self):
        command = triage.repro_command(
            "id with space", "/tmp/a b/corpus.json",
            driver="/tmp/a b/x86-mir-run")
        self.assertIn("'id with space'", command)
        self.assertIn("'/tmp/a b/x86-mir-run'", command)


if __name__ == "__main__":
    unittest.main()
