"""The AArch64 20-atom smoke fixture and its sidecar run."""

import json
import os
import subprocess
import unittest

import testlib

FIXTURE = os.path.join(testlib.repo_root(), "tests", "fixtures",
                       "aarch64-smoke.json")


def driver_path():
    explicit = os.environ.get("X86_MIR_CORPUS_DRIVER")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    path = os.path.join(testlib.repo_root(), ".build", "x86-mir-run")
    return path if os.path.exists(path) else None


class SmokeFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as handle:
            cls.doc = json.load(handle)

    def test_fixture_shape_and_anchors(self):
        self.assertEqual(self.doc["schema"],
                         "llvm-x86-legalizer-cpp-corpus")
        self.assertEqual(self.doc["config_descriptor"]["target"], "aarch64")
        self.assertEqual(self.doc["atom_count"], 20)
        self.assertEqual(len(self.doc["atoms"]), 20)
        ids = [atom["id"] for atom in self.doc["atoms"]]
        self.assertEqual(len(set(ids)), 20)
        by_opcode = {}
        for atom in self.doc["atoms"]:
            by_opcode.setdefault(atom["opcode"], []).append(atom)
        self.assertIn(["s32"], [a["query"]["types"]
                                for a in by_opcode["G_ADD"]])
        constants = {a["query"]["types"][0]
                     for a in by_opcode["G_FCONSTANT"]}
        self.assertTrue({"f16", "bf16"} <= constants)
        extracts = [a for a in by_opcode["G_EXTRACT_VECTOR_ELT"]
                    if len(a["query"]["types"]) == 3]
        self.assertTrue(extracts)


class SmokeRunTest(unittest.TestCase):
    def test_sidecar_runs_all_twenty_atoms(self):
        driver = driver_path()
        if driver is None:
            self.skipTest("sidecar driver not built (set X86_MIR_CORPUS_DRIVER)")
        out = os.path.join(testlib.repo_root(), ".omx", "tmp",
                           "aarch64-smoke.jsonl")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        env = dict(os.environ,
                   X86_LEGALIZER_MIR_CORPUS_IN=FIXTURE,
                   X86_LEGALIZER_MIR_OUT=out,
                   X86_LEGALIZER_MIR_RULES="production",
                   X86_LEGALIZER_MIR_MAX_STEPS="2000")
        proc = subprocess.run([driver], env=env, capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcomes, summary = [], None
        with open(out, encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("event") == "outcome":
                    outcomes.append(event)
                elif event.get("event") == "summary":
                    summary = event
        self.assertIsNotNone(summary)
        self.assertEqual(summary["atom_count"], 20)
        self.assertEqual(len(outcomes), 20)
        counts = summary["counts"]
        self.assertEqual(sum(counts.values()), 20)
        for kind in ("crash", "hang", "budget", "not_run"):
            self.assertNotIn(kind, counts)
        self.assertNotEqual(summary["config"], "")
        by_id = {outcome["id"]: outcome for outcome in outcomes}
        with open(FIXTURE, encoding="utf-8") as handle:
            fixture = json.load(handle)
        s32_add = next(atom["id"] for atom in fixture["atoms"]
                       if atom["opcode"] == "G_ADD"
                       and atom["query"]["types"] == ["s32"])
        self.assertEqual(by_id[s32_add]["outcome"], "legalized")
        for outcome in outcomes:
            if outcome["opcode"] == "G_FCONSTANT":
                self.assertNotEqual(outcome["outcome"], "unbuildable")
        extract = next(o for o in outcomes
                       if o["opcode"] == "G_EXTRACT_VECTOR_ELT"
                       and o["outcome"] == "legalized")
        self.assertIsNotNone(extract)
        self.assertEqual(counts.get("construction_mismatch", 0), 0)


if __name__ == "__main__":
    unittest.main()
