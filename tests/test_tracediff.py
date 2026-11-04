import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import tracediff


def step(action, idx=None, to=None, kind=None):
    out = {"action": action}
    if idx is not None:
        out["idx"] = idx
    if to is not None:
        out["to"] = to
    if kind is not None:
        out["answer_kind"] = kind
    return out


def atom(atom_id, status="legal", rounds=1, final=("s32",), trace=None,
         config="C2"):
    return {
        "id": atom_id,
        "config": config,
        "opcode": atom_id.split("/")[0],
        "rule_set": atom_id.split("/")[0],
        "rounds": rounds,
        "status": status,
        "final_types": list(final),
        "trace": trace if trace is not None else [step("Legal")],
    }


def trace(atoms, oracle="reference", config="C2", sha="deadbeef"):
    return {
        "schema": "llvm-x86-legalizer-cpp-trace",
        "version": 2,
        "oracle": {"kind": oracle},
        "config": config,
        "corpus_sha256": sha,
        "atoms": atoms,
    }


class TraceDiffTest(unittest.TestCase):
    def test_identical_traces_have_no_diffs(self):
        doc = trace([atom("X86_ADD#1"), atom("X86_LOAD#2")])
        report = tracediff.compare(doc, doc)
        self.assertEqual(report["diff_count"], 0)
        self.assertEqual(report["ids"]["common"], 2)
        self.assertTrue(report["corpus_match"])
        self.assertFalse(tracediff.has_differences(report))

    def test_answer_kind_is_not_a_difference(self):
        left = trace([atom("X86_ADD#1", trace=[step("Legal")])])
        right = trace([atom("X86_ADD#1", trace=[step("Legal", kind="hit")])],
                      oracle="table")
        report = tracediff.compare(left, right)
        self.assertEqual(report["diff_count"], 0)
        self.assertEqual(report["answer_kinds"]["right"], {"hit": 1})

    def test_status_and_final_state_are_compared(self):
        left = trace([atom("X86_ADD#1")])
        right = trace([atom("X86_ADD#1", status="unsupported",
                            final=("s64",), trace=[step("Unsupported")])],
                      oracle="table")
        report = tracediff.compare(left, right)
        self.assertEqual(report["diff_count"], 1)
        self.assertEqual(report["classes"][0]["transition"],
                         "legal -> unsupported")
        entry = report["diffs"][0]
        self.assertEqual(entry["first_divergence"], 0)
        self.assertEqual(entry["divergence"]["left_step"]["action"], "Legal")

    def test_multi_round_divergence_index(self):
        left = trace([atom("X86_ANYEXT#1", rounds=3,
                           final=("s32",),
                           trace=[step("WidenScalar", 1, "s64"),
                                  step("NarrowScalar", 1, "s32"),
                                  step("Legal")])])
        right = trace([atom("X86_ANYEXT#1", rounds=2,
                            final=("s64",),
                            trace=[step("WidenScalar", 1, "s64"),
                                   step("Legal")])],
                      oracle="table")
        report = tracediff.compare(left, right)
        entry = report["diffs"][0]
        self.assertEqual(entry["first_divergence"], 1)
        self.assertEqual(entry["divergence"]["left_step"]["action"],
                         "NarrowScalar")
        self.assertEqual(entry["divergence"]["right_step"]["action"], "Legal")

    def test_missing_ids_are_reported(self):
        left = trace([atom("X86_ADD#1"), atom("X86_SUB#1")])
        right = trace([atom("X86_ADD#1")], oracle="table")
        report = tracediff.compare(left, right)
        self.assertEqual(report["ids"]["left_only"], 1)
        self.assertEqual(report["ids"]["left_only_sample"], ["X86_SUB#1"])
        self.assertTrue(tracediff.has_differences(report))

    def test_duplicate_ids_are_counted(self):
        dup = [atom("X86_ADD#1"), atom("X86_ADD#1")]
        report = tracediff.compare(trace(dup), trace(dup))
        self.assertEqual(report["duplicate_ids"], {"left": 1, "right": 1})


if __name__ == "__main__":
    unittest.main()
