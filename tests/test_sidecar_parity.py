"""Parity tests between the out-of-tree sidecar and the in-tree drivers.

These are integration tests.  They skip unless the sidecar has been built and
an LLVM build/corpus directory is available:

    X86_MIR_SIDECAR_BUILD=<sidecar build dir>     # default <repo>/.build
    X86_MIR_LLVM_BUILD=<llvm build dir>           # for the legacy X86Tests
    X86_MIR_CORPUS_DIR=<dir with up-C{0..3}.json> # cached corpora

The pinned-baseline test is additionally opt-in through ``X86_MIR_PARITY=1``:
it compares against a witness recorded on one LLVM revision, so it must not
fail on a checkout whose LLVM has drifted.  See ``docs/PRODUCTIZATION.md``.
"""

import json
import os
import subprocess
import tempfile
import unittest

import testlib

CORPUS_DIR = testlib.corpus_dir()
SIDECAR_RUN = testlib.sidecar_binary("x86-mir-run")
SIDECAR_ORACLE = testlib.sidecar_binary("x86-mir-oracle")
IN_TREE = testlib.in_tree_driver()
BASELINE = os.path.join(testlib.TOOL_DIR, "src", "x86_mir_corpus", "assets",
                        "mir-baseline-witness.json")
PARITY_ENABLED = os.environ.get("X86_MIR_PARITY") == "1"


def corpus(name):
    if not CORPUS_DIR:
        return None
    path = os.path.join(CORPUS_DIR, name)
    return path if os.path.exists(path) else None


def run_tool(*args):
    return testlib.run_cli(*args)


class OracleParityTest(unittest.TestCase):
    def test_sidecar_oracle_matches_in_tree_filler(self):
        points = corpus("up-C2.json.points.json")
        if points is None or SIDECAR_ORACLE is None or IN_TREE is None:
            self.skipTest("sidecar oracle, X86Tests, or corpus is missing")
        with tempfile.TemporaryDirectory() as tmp:
            expected = os.path.join(tmp, "intree.json")
            actual = os.path.join(tmp, "sidecar.json")
            env = dict(os.environ)
            env["X86_LEGALIZER_CPP_POINTS"] = points
            env["X86_LEGALIZER_CPP_CORPUS_OUT"] = expected
            proc = subprocess.run(
                [IN_TREE, "--gtest_also_run_disabled_tests",
                 "--gtest_filter=*GenerateCorpusFromCppPoints*"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            env["X86_LEGALIZER_CPP_CORPUS_OUT"] = actual
            proc = subprocess.run([SIDECAR_ORACLE], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(expected, "rb") as handle:
                expected_bytes = handle.read()
            with open(actual, "rb") as handle:
                actual_bytes = handle.read()
        self.assertEqual(expected_bytes, actual_bytes)


class MirParityTest(unittest.TestCase):
    def run_report(self, driver, source, out):
        proc = run_tool("mir-run", "--corpus", source, "--driver", driver,
                        "--timeout", "30", "--out", out)
        # Exit 1 means the report is complete but A3 (zero new findings) is
        # not met; that is the expected state of the current baseline.
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        with open(out, encoding="utf-8") as handle:
            return json.load(handle)

    def test_sidecar_mir_matches_in_tree_driver_c2(self):
        source = corpus("up-C2.json")
        if source is None or SIDECAR_RUN is None or IN_TREE is None:
            self.skipTest("sidecar driver, X86Tests, or corpus is missing")
        with tempfile.TemporaryDirectory() as tmp:
            reference = self.run_report(IN_TREE, source,
                                        os.path.join(tmp, "intree.json"))
            actual = self.run_report(SIDECAR_RUN, source,
                                     os.path.join(tmp, "sidecar.json"))
        self.assertEqual(reference["counts"], actual["counts"])
        left = {a["id"]: a for a in reference["atoms"]}
        right = {a["id"]: a for a in actual["atoms"]}
        self.assertEqual(set(left), set(right))
        fields = ("outcome", "steps", "detail_code", "failed_on_opcode")
        diffs = [(atom, field, left[atom].get(field), right[atom].get(field))
                 for atom in left for field in fields
                 if left[atom].get(field) != right[atom].get(field)]
        self.assertEqual(diffs, [])

    def test_four_config_counts_match_baseline(self):
        if not PARITY_ENABLED:
            self.skipTest(
                "pinned-baseline parity is opt-in; set X86_MIR_PARITY=1 and "
                "use the LLVM revision the witness was recorded on")
        if SIDECAR_RUN is None:
            self.skipTest("sidecar driver is missing")
        with open(BASELINE, encoding="utf-8") as handle:
            baseline = json.load(handle)["configs"]
        missing = [config for config in baseline
                   if corpus(f"up-{config}.json") is None]
        if missing:
            self.skipTest("corpus is missing for " + ", ".join(missing))
        with tempfile.TemporaryDirectory() as tmp:
            for config, expected in sorted(baseline.items()):
                source = corpus(f"up-{config}.json")
                out = os.path.join(tmp, f"{config}.json")
                report = self.run_report(SIDECAR_RUN, source, out)
                self.assertEqual(report["counts"], expected["counts"], config)
                self.assertEqual(report["corpus"]["atoms"],
                                 expected["corpus"]["atoms"], config)
                crashes = sorted(a["id"] for a in report["atoms"]
                                 if a["outcome"] == "crash")
                self.assertEqual(crashes, expected["crash"]["atom_ids"], config)


if __name__ == "__main__":
    unittest.main()
