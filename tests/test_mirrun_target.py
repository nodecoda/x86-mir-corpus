"""Target-aware resolution in the MIR corpus supervisor."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import testlib
from x86_mir_corpus import mirrun

SMOKE = os.path.join(testlib.repo_root(), "tests", "fixtures",
                     "aarch64-smoke.json")


def driver_path():
    explicit = os.environ.get("X86_MIR_CORPUS_DRIVER")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    path = os.path.join(testlib.repo_root(), ".build", "x86-mir-run")
    return path if os.path.exists(path) else None


def aarch64_corpus(count=2, config="A2"):
    atoms = [{
        "id": f"a64/{index}",
        "origin": "test",
        "rule_set": "AArch64_smoke",
        "opcode": "G_ADD",
        "rule_index": None,
        "config": config,
        "query": {"types": ["s32"]},
    } for index in range(count)]
    return {
        "schema": "llvm-x86-legalizer-cpp-corpus",
        "version": 2,
        "config": config,
        "config_descriptor": {"target": "aarch64", "id": config,
                              "features": {}},
        "atom_count": count,
        "atoms": atoms,
    }


def x86_corpus(count=2, config="C2"):
    atoms = [{
        "id": f"x86/{index}",
        "origin": "test",
        "rule_set": "X86_ADD",
        "opcode": "G_ADD",
        "rule_index": None,
        "config": config,
        "query": {"types": ["s32"]},
    } for index in range(count)]
    return {
        "schema": "llvm-x86-legalizer-cpp-corpus",
        "version": 2,
        "config": config,
        "config_descriptor": {"id": config, "features": {}},
        "atom_count": count,
        "atoms": atoms,
    }


def result(index, outcome="legalized", atom_id=None):
    return {
        "id": atom_id or f"a64/{index}",
        "index": index,
        "opcode": "G_ADD",
        "outcome": outcome,
        "steps": 1,
        "detail_code": "",
        "detail": "",
        "stderr_tail": "",
    }


class CorpusTargetTest(unittest.TestCase):
    def test_target_defaults_to_x86_without_a_descriptor(self):
        self.assertEqual(mirrun.corpus_target({}), "x86")

    def test_target_is_read_from_the_descriptor(self):
        self.assertEqual(mirrun.corpus_target(aarch64_corpus()), "aarch64")

    def test_validate_accepts_the_aarch64_smoke_fixture(self):
        doc = mirrun.load_corpus(SMOKE)
        atoms, config = mirrun.validate_corpus(doc)
        self.assertEqual(len(atoms), 20)
        self.assertEqual(config, "A2")

    def test_a2_pointer_width_is_64(self):
        corpus = aarch64_corpus(count=1)
        corpus["atoms"][0]["query"]["types"] = ["p0:32"]
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    def test_aarch64_accepts_the_native_64_bit_spelling(self):
        corpus = aarch64_corpus(count=1)
        corpus["atoms"][0]["query"]["types"] = ["p0"]
        atoms, config = mirrun.validate_corpus(corpus)
        self.assertEqual(len(atoms), 1)
        self.assertEqual(config, "A2")

    def test_unknown_config_is_rejected_for_the_target(self):
        corpus = aarch64_corpus(config="C2")
        with self.assertRaises(mirrun.MirRunInfraError) as caught:
            mirrun.validate_corpus(corpus)
        self.assertIn("aarch64", str(caught.exception))

    def test_unknown_target_is_rejected(self):
        corpus = aarch64_corpus()
        corpus["config_descriptor"]["target"] = "riscv64"
        with self.assertRaises(mirrun.MirRunInfraError):
            mirrun.validate_corpus(corpus)

    def test_x86_corpus_without_target_still_validates(self):
        atoms, config = mirrun.validate_corpus(x86_corpus())
        self.assertEqual(len(atoms), 2)
        self.assertEqual(config, "C2")


class KnownFamilyTest(unittest.TestCase):
    def build_vector_atom(self):
        return {
            "id": "x",
            "rule_set": "X86_BUILD_VECTOR",
            "config": "C2",
            "query": {"types": ["v3p0"]},
            "expected": {"action": "MoreElements"},
        }

    def test_x86_family_is_unchanged(self):
        self.assertTrue(mirrun.is_known_family(self.build_vector_atom(), "C2"))

    def test_aarch64_has_no_known_family_yet(self):
        self.assertFalse(mirrun.is_known_family(
            self.build_vector_atom(), "A2", "aarch64"))
        self.assertEqual(mirrun.aarch64_known_families(), frozenset())

    def test_aarch64_reviewed_family_asset_is_the_seam(self):
        atom = dict(self.build_vector_atom(), rule_set="AArch64_ADD")
        saved = mirrun._AARCH64_FAMILIES
        try:
            mirrun._AARCH64_FAMILIES = frozenset({"AArch64_ADD"})
            self.assertTrue(mirrun.is_known_family(atom, "A2", "aarch64"))
            self.assertFalse(mirrun.is_known_family(
                self.build_vector_atom(), "A2", "aarch64"))
        finally:
            mirrun._AARCH64_FAMILIES = saved


class ReportTargetTest(unittest.TestCase):
    def test_report_records_the_corpus_target(self):
        corpus = aarch64_corpus(count=2)
        report = mirrun.build_report(
            corpus, [result(0), result(1)], "/tmp/corpus.json", "sha",
            "production", 10000, 120, 2000)
        self.assertEqual(report["target"], "aarch64")
        self.assertEqual(report["corpus"]["target"], "aarch64")
        self.assertEqual(sum(report["counts"].values()), 2)


class DriverCommandTest(unittest.TestCase):
    def test_sidecar_names_run_directly(self):
        self.assertEqual(mirrun.driver_command("/x/mir-run"), ["/x/mir-run"])
        self.assertEqual(mirrun.driver_command("/x/x86-mir-run"),
                         ["/x/x86-mir-run"])

    def test_gtest_binary_keeps_the_legacy_filter(self):
        command = mirrun.driver_command("/x/X86Tests")
        self.assertEqual(command[0], "/x/X86Tests")
        self.assertIn("--gtest_filter=*RunCorpusThroughLegalizer", command)


class SmokeRunTest(unittest.TestCase):
    def test_cli_runs_the_aarch64_smoke(self):
        driver = driver_path()
        if driver is None:
            self.skipTest("sidecar driver not built (set X86_MIR_CORPUS_DRIVER)")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                [os.path.join(testlib.repo_root(), "src"),
                 env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
            proc = subprocess.run(
                [sys.executable, "-m", "x86_mir_corpus", "mir-run",
                 "--corpus", SMOKE, "--driver", driver, "--out", out],
                cwd=testlib.repo_root(), env=env,
                capture_output=True, text=True)
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            self.assertNotIn("unknown corpus config", proc.stderr)
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["target"], "aarch64")
            self.assertEqual(report["corpus"]["target"], "aarch64")
            self.assertEqual(sum(report["counts"].values()), 20)

    def test_explicit_target_mismatch_is_an_infra_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "corpus.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(aarch64_corpus(), handle)
            with self.assertRaises(mirrun.MirRunInfraError):
                mirrun.run(path, os.path.join(tmp, "out.json"), driver="/bin/true",
                           target="x86")

    def test_reference_rules_are_x86_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "corpus.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(aarch64_corpus(), handle)
            with self.assertRaises(mirrun.MirRunInfraError):
                mirrun.run(path, os.path.join(tmp, "out.json"), driver="/bin/true",
                           rules="reference")


if __name__ == "__main__":
    unittest.main()
