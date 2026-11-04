"""Static construction checks mirrored from LegalizerInfo.cpp's verifier."""

import os
import unittest

import testlib
from x86_mir_corpus import domain, dsl, parse, targets

OUT_OF_RANGE = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf([](const LegalityQuery &Q) { return true; },
                     changeTo(5, LLT::scalar(64)));
}
"""

IMM_OUT_OF_RANGE = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_PREFETCH)
      .legalFor({LLT::scalar(32)})
      .immIdx(7);
}
"""

IMM_COVERED = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_PREFETCH).custom();
}
"""


def source(text):
    return {"rev": None, "path": "<test>", "sha256": "test",
            "lines": text.count("\n") + 1}


def aarch64(text):
    profile = targets.get("aarch64")
    return parse.parse_source(text, source(text), "A2",
                              profile.configs["A2"], profile.rule_set_prefix,
                              profile.llt_extended)


class OpcodeArityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = dsl.load()

    def test_dense_type_and_imm_indices_come_from_generic_opcodes(self):
        arity = dsl.opcode_arity(self.table)
        # G_SMIN names type0 in both operands: one legality type index.
        self.assertEqual(arity["G_SMIN"], (1, 0))
        self.assertEqual(arity["G_LOAD"], (2, 0))
        self.assertEqual(arity["G_MEMSET"], (3, 1))
        self.assertEqual(arity["G_PREFETCH"], (1, 3))
        self.assertEqual(self.table["counts"]["opcodes"], 234)


class ConstructionIssueTest(unittest.TestCase):
    def test_mutation_type_index_out_of_range(self):
        issues = domain.construction_issues(aarch64(OUT_OF_RANGE))
        codes = [issue["detail_code"] for issue in issues]
        self.assertIn("mutation_type_idx", codes)

    def test_imm_index_out_of_range_and_coverage(self):
        issues = domain.construction_issues(aarch64(IMM_OUT_OF_RANGE))
        codes = [issue["detail_code"] for issue in issues]
        self.assertIn("imm_idx", codes)
        self.assertIn("imm_coverage", codes)

    def test_catch_all_method_discharges_imm_coverage(self):
        codes = [issue["detail_code"]
                 for issue in domain.construction_issues(aarch64(IMM_COVERED))]
        self.assertNotIn("imm_coverage", codes)

    def test_real_sources_only_report_shape_gaps(self):
        for target, expected in (("x86", 4), ("aarch64", 16)):
            profile = targets.get(target)
            path = os.path.join(
                testlib.SRC_DIR, "x86_mir_corpus", "assets",
                "source-snapshots",
                "fed20e0e-" + ("X86" if target == "x86" else "AArch64")
                + "LegalizerInfo.cpp")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            result = parse.parse_source(
                text, source(text), profile.default_config,
                profile.configs[profile.default_config],
                profile.rule_set_prefix, profile.llt_extended)
            issues = domain.construction_issues(result)
            self.assertEqual(len(issues), expected, target)
            self.assertEqual({issue["detail_code"] for issue in issues},
                             {"shape_arity"}, target)


if __name__ == "__main__":
    unittest.main()
