"""Tests for the legalizer DSL symbol table and its drift gate."""

import json
import os
import subprocess
import sys
import unittest

import testlib
from x86_mir_corpus import dsl


class SymbolTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = dsl.load()

    def test_counts(self):
        self.assertEqual(self.table["version"], 2)
        self.assertEqual(self.table["counts"]["methods"], 66)
        self.assertEqual(self.table["counts"]["predicates"], 34)
        self.assertEqual(self.table["counts"]["mutations"], 8)
        self.assertEqual(self.table["counts"]["actions"], 10)
        self.assertEqual(self.table["counts"]["llt"], 17)

    def test_v2_sections_are_populated(self):
        counts = self.table["counts"]
        self.assertEqual(counts["llt_queries"], 53)
        self.assertEqual(counts["element_count"], 20)
        self.assertEqual(counts["query_api"], 8)
        self.assertEqual(counts["legalizer_info_api"], 12)
        self.assertEqual(counts["helper_entrypoints"], 8)
        self.assertEqual(counts["enums"], 2)
        self.assertEqual(counts["namespaces"], 6)
        self.assertEqual(counts["target_functions"], 34)
        self.assertEqual(counts["target_queries"], 5)
        self.assertEqual(counts["ignore"], 15)
        self.assertEqual(counts["opcodes"], 234)

    def test_llt_queries_cover_the_aarch64_lambda_surface(self):
        for name in ("getScalarType", "isFloat16", "isBFloat16", "divide",
                     "multiplyElements", "getNumElements", "getElementCount",
                     "changeElementSize", "changeVectorElementType"):
            entry = dsl.llt_query_entry(name, self.table)
            self.assertIsNotNone(entry, name)
            self.assertEqual(entry["resolution"], "exact", name)
        self.assertEqual(
            dsl.llt_query_entry("isFloat16", self.table)["returns"], "bool")
        self.assertEqual(
            dsl.llt_query_entry("getSizeInBits", self.table)["returns"],
            "typesize")

    def test_mutation_definitions_are_pinned(self):
        self.assertEqual(len(self.table["source_files"]), 10)
        for entry in self.table["mutations"]:
            self.assertRegex(entry["definition"],
                             r"^LegalizeMutations\.cpp:\d+$", entry["name"])

    def test_query_fields_and_enum_values(self):
        names = dsl.query_api_names(self.table)
        for name in ("Types", "MMODescrs", "Immediates", "MemDesc.MemoryTy"):
            self.assertIn(name, names)
        self.assertEqual(dsl.enum_values("CodeModel", self.table),
                         ["Tiny", "Small", "Kernel", "Medium", "Large"])
        self.assertEqual(dsl.enum_values("LegalizeResult", self.table),
                         ["AlreadyLegal", "Legalized", "UnableToLegalize"])

    def test_helper_entrypoints_map_actions(self):
        entries = {entry["name"]: entry["action"]
                   for entry in self.table["helper_entrypoints"]}
        self.assertEqual(entries["lower"], "Lower")
        self.assertEqual(entries["fewerElementsVector"], "FewerElements")
        self.assertEqual(entries["legalizeInstrStep"], "dispatch")

    def test_target_source_vocabulary(self):
        self.assertIn("LegalizeMutations",
                      dsl.namespace_names(self.table))
        functions = dsl.target_function_names(self.table)
        self.assertIn("AArch64LegalizerInfo::legalizeCustom", functions)
        self.assertIn("matchLDPSTPAddrMode", functions)

    def test_every_chain_method_is_classified(self):
        for entry in self.table["methods"]:
            self.assertNotEqual(entry["kind"], "unknown", entry["name"])
            self.assertNotIn(entry["parser"], ("none", ""), entry["name"])
            self.assertIn(entry["domain"], ("exact", "opaque"), entry["name"])

    def test_aarch64_only_methods_are_present(self):
        names = dsl.method_names(self.table)
        for name in ("minScalarOrEltIf", "unsupportedIf",
                     "unsupportedIfMemSizeNotPow2", "widenScalarOrEltToNextPow2OrMinSize",
                     "minScalarEltSameAsIf", "immIdx", "custom", "unsupported"):
            self.assertIn(name, names)

    def test_roles_are_extracted_from_the_signature(self):
        entry = dsl.method_entry("minScalarOrEltIf", self.table)
        self.assertTrue(entry["predicate_gated"])
        self.assertEqual(entry["slot_args"], ["TypeIdx"])
        self.assertEqual(entry["overloads"][0]["roles"],
                         ["predicate", "slot", "llt"])
        pair = next(p for p in self.table["predicates"]
                    if p["name"] == "typePairInSet")
        self.assertEqual(pair["overloads"][0]["roles"][:2], ["slot", "slot"])

    def test_actions_match_the_enum(self):
        self.assertEqual(
            [entry["name"] for entry in self.table["actions"]],
            ["Legal", "NarrowScalar", "WidenScalar", "FewerElements",
             "MoreElements", "Bitcast", "Lower", "Libcall", "Custom",
             "Unsupported"])

    def test_scalable_vectors_are_first_class(self):
        entry = next(e for e in self.table["llt_constructors"]
                     if e["name"] == "scalable_vector")
        self.assertEqual(entry["corpus_spelling"], "nxv<lanes><elem>")
        self.assertTrue(entry["syntax"])
        bf16 = next(e for e in self.table["llt_constructors"]
                    if e["name"] == "bfloat16")
        self.assertEqual(bf16["corpus_spelling"], "bf16")


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.repo = testlib.llvm_root()
        if not self.repo:
            self.skipTest("no LLVM checkout (set X86_MIR_LLVM_SRC)")
        proc = subprocess.run(
            ["git", "-C", self.repo, "cat-file", "-e",
             f"{dsl.load()['revision']}^{{commit}}"],
            capture_output=True)
        if proc.returncode != 0:
            self.skipTest("pinned revision is not available in the checkout")

    def test_checked_in_table_matches_the_pinned_headers(self):
        report = dsl.verify(repo=self.repo,
                            revision=dsl.load()["revision"])
        self.assertTrue(report["ok"], report["reasons"])

    def test_a_missing_mutation_is_reported(self):
        table = json.loads(json.dumps(dsl.load()))
        table["mutations"] = [m for m in table["mutations"]
                              if m["name"] != "changeElementSizeTo"]
        report = dsl.verify(repo=self.repo,
                            revision=table["revision"], table=table)
        self.assertFalse(report["ok"])
        self.assertIn("mutations-names", report["reasons"])

    def test_a_v1_table_is_rejected(self):
        table = json.loads(json.dumps(dsl.load()))
        table["version"] = 1
        report = dsl.verify(repo=self.repo,
                            revision=table["revision"], table=table)
        self.assertFalse(report["ok"])
        self.assertIn("table-version", report["reasons"])

    def test_a_missing_method_is_reported(self):
        table = json.loads(json.dumps(dsl.load()))
        table["methods"] = [m for m in table["methods"]
                            if m["name"] != "minScalarOrEltIf"]
        report = dsl.verify(repo=self.repo,
                            revision=table["revision"], table=table)
        self.assertFalse(report["ok"])
        self.assertIn("methods-names", report["reasons"])
        self.assertTrue(any(issue.get("name") == "minScalarOrEltIf"
                            for issue in report["issues"]))

    def test_cli_verify_passes(self):
        proc = subprocess.run(
            [sys.executable, "-m", "x86_mir_corpus", "dsl", "verify",
             "--repo", self.repo],
            cwd=testlib.repo_root(), capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=testlib.SRC_DIR))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dsl symbols: ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
