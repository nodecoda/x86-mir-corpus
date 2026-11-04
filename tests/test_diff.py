import contextlib
import io
import json
import os
import tempfile
import unittest

import testlib
from x86_mir_corpus import cli, corpusdiff


def atom(opcode, types, expected=None, rule_set="X86_ADD", mem=None,
         align=0, origin="rule", rule_index=0):
    query = {"types": list(types)}
    if mem is not None:
        query["mem"] = {"type": mem, "align": align}
    out = {
        "id": f"cpp/{rule_set}/{opcode}/{origin}/{rule_index}",
        "origin": origin,
        "rule_set": rule_set,
        "opcode": opcode,
        "rule_index": rule_index,
        "config": "C2",
        "query": query,
    }
    if expected is not None:
        out["expected"] = expected
    return out


def descriptor():
    return {"id": "C2", "triple": "x86_64", "features": {"is64Bit": True}}


def corpus(atoms, desc=None):
    return {
        "schema": "llvm-x86-legalizer-cpp-corpus",
        "version": 2,
        "config_descriptor": desc or descriptor(),
        "atom_count": len(atoms),
        "atoms": atoms,
    }


class CorpusDiffTest(unittest.TestCase):
    def test_identical_corpora_have_no_differences(self):
        doc = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})])
        report = corpusdiff.compare(doc, doc)
        self.assertTrue(report["config_match"])
        self.assertTrue(report["expected_available"])
        self.assertEqual(report["expected_compared"], 1)
        self.assertEqual(report["expected_diff"], [])
        self.assertFalse(corpusdiff.has_differences(report))

    def test_expected_action_difference_is_reported(self):
        left = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})])
        right = corpus([atom("G_ADD", ["s32"], {"action": "Custom", "idx": 0,
                                               "to": "s32"})])
        report = corpusdiff.compare(left, right)
        self.assertEqual(len(report["expected_diff"]), 1)
        self.assertEqual(report["expected_diff"][0]["query"],
                         {"config": "C2", "opcode": "G_ADD",
                          "types": ["s32"]})
        self.assertTrue(corpusdiff.has_differences(report))

    def test_shape_only_corpus_never_invents_expected_parity(self):
        shapes = corpus([atom("G_ADD", ["s32"])])
        filled = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})])
        report = corpusdiff.compare(shapes, filled)
        self.assertFalse(report["expected_available"])
        self.assertEqual(report["expected_diff"], [])
        self.assertFalse(corpusdiff.has_differences(report))

    def test_left_and_right_only_keys_and_per_rule_coverage(self):
        left = corpus([atom("G_ADD", ["s32"], {"action": "Legal"}),
                       atom("G_ADD", ["s64"], {"action": "Legal"},
                            rule_set="X86_ADD")])
        right = corpus([atom("G_ADD", ["s32"], {"action": "Legal"}),
                        atom("G_CTPOP", ["s32"], {"action": "Legal"},
                             rule_set="X86_CTPOP")])
        report = corpusdiff.compare(left, right)
        self.assertEqual(report["keys"]["common"], 1)
        self.assertEqual(len(report["left_only"]), 1)
        self.assertEqual(report["left_only"][0]["types"], ["s64"])
        self.assertEqual(len(report["right_only"]), 1)
        self.assertEqual(report["right_only"][0]["opcode"], "G_CTPOP")
        self.assertEqual(report["per_rule_coverage"]["right_only"],
                         ["X86_CTPOP"])
        self.assertIn({"rule_set": "X86_ADD", "left": 2, "right": 1},
                      report["per_rule_coverage"]["count_diff"])

    def test_pointer_spelling_is_normalized(self):
        left = corpus([atom("G_LOAD", ["p0"], {"action": "Legal"},
                            mem="s8", align=1)])
        right = corpus([atom("G_LOAD", ["p0:64"], {"action": "Legal"},
                             mem="s8", align=1)])
        report = corpusdiff.compare(left, right)
        self.assertEqual(report["keys"]["left_only"], 0)
        self.assertEqual(report["keys"]["right_only"], 0)
        self.assertEqual(report["expected_compared"], 1)

    def test_v1_atoms_without_opcode_or_descriptor_are_readable(self):
        v1 = {"schema": "llvm.gisel.x86-legalizer-witness", "version": 1,
              "atoms": [{
                  "id": "x86/rule/X86_ADD#0", "origin": "rule",
                  "rule_set": "X86_ADD", "rule_index": 0, "config": "C2",
                  "query": {"types": ["s32"]},
                  "expected": {"action": "Legal"},
              }]}
        v2 = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})])
        self.assertEqual(corpusdiff.config_id(v1), "C2")
        report = corpusdiff.compare(v1, v2)
        self.assertTrue(report["config_match"])
        self.assertEqual(report["keys"]["common"], 1)
        self.assertEqual(report["expected_diff"], [])
        self.assertEqual(report["unmapped_rule_sets"], [])

    def test_mixed_config_v1_document_matches_itself(self):
        v1 = {"schema": "llvm.gisel.x86-legalizer-witness", "version": 1,
              "atoms": [atom("G_ADD", ["s32"], {"action": "Legal"}),
                        atom("G_SUB", ["s64"], {"action": "Legal"},
                             rule_set="X86_SUB")]}
        v1["atoms"][0]["config"] = "C2"
        v1["atoms"][1]["config"] = "C1"
        report = corpusdiff.compare(v1, v1)
        self.assertTrue(report["config_match"])
        self.assertFalse(corpusdiff.has_differences(report))

    def test_committed_witness_is_a_v1_document(self):
        path = testlib.witness_path()
        if path is None:
            self.skipTest("committed witness is not available")
        doc = corpusdiff.load(path)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(corpusdiff.config_id(doc), None)
        report = corpusdiff.compare(doc, doc)
        self.assertTrue(report["config_match"])
        self.assertTrue(report["expected_available"])
        self.assertFalse(corpusdiff.has_differences(report))

    def test_config_descriptor_mismatch_is_a_difference(self):
        left = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})])
        other = {"id": "C3", "triple": "i386", "features": {"is64Bit": False}}
        right = corpus([atom("G_ADD", ["s32"], {"action": "Legal"})], other)
        report = corpusdiff.compare(left, right)
        self.assertFalse(report["config_match"])
        self.assertTrue(corpusdiff.has_differences(report))


class CorpusDiffCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, doc):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def test_diff_cli_exit_codes(self):
        left = self.write("left.json",
                          corpus([atom("G_ADD", ["s32"],
                                       {"action": "Legal"})]))
        right = self.write("right.json",
                           corpus([atom("G_ADD", ["s32"],
                                        {"action": "Custom", "idx": 0,
                                         "to": "s32"})]))
        out = os.path.join(self.tmp.name, "diff.json")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["diff", "--left", left, "--right", right, "--out", out])
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["schema"], "llvm-x86-legalizer-corpus-diff")
        self.assertEqual(len(report["expected_diff"]), 1)

        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["diff", "--left", left, "--right", right,
                      "--out", out, "--strict"])
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
