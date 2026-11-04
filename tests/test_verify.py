import os
import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import parse
from x86_mir_corpus import verify
from x86_mir_corpus.target import CONFIGS


MINI = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({{s32, s32}})
      .legalIf([](unsigned, LLT, const LegalityQuery &Q) {
        return typeIs(0, s8);
      });
}
"""

TD_INC = """\
X86TestTypeClasses[] = {
  {X86TestKind_Scalar, 8, 0},
  {X86TestKind_Scalar, 32, 0},
  {X86TestKind_Pointer, 0, 1},
};
X86Tables[] = {
  /*0*/ {"X86_ADD", "G_ADD", {0}, {24}, false, 0, 3, 1}, // X86_ADD
};
"""


def build_doc(src=MINI):
    source = {"rev": None, "path": "<test>", "sha256": "test", "lines": 1}
    result = parse.parse_source(src, source, "C2", CONFIGS["C2"])
    from x86_mir_corpus import emit
    return emit.ast_document(result, source)


class VerifyTest(unittest.TestCase):
    def test_parse_td_inc_reads_rows_and_classes(self):
        td = verify.parse_td_inc(TD_INC)
        self.assertEqual(td["rows"], [{
            "name": "X86_ADD", "opcode": "G_ADD",
            "num_rules": 3, "num_slots": 1,
        }])
        self.assertEqual(td["classes"], ["s8", "s32", "p0"])

    def test_lambda_expansion_is_a_reviewed_delta(self):
        report = verify.build_report(build_doc(), verify.parse_td_inc(TD_INC))
        self.assertEqual(report["rule_ledger"][0]["class"], "td_expands_lambda")
        known = {("X86_ADD", "td_expands_lambda")}
        self.assertEqual(verify.only_allowed_deltas(report, known), [])

    def test_unregistered_delta_is_reported(self):
        report = verify.build_report(build_doc(), verify.parse_td_inc(TD_INC))
        bad = verify.only_allowed_deltas(report, set())
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["rule_set"], "X86_ADD")

    def test_td_only_delta_is_reported(self):
        src = MINI.replace("      .legalIf", "      // legalIf")
        report = verify.build_report(build_doc(src), verify.parse_td_inc(TD_INC))
        self.assertEqual(report["rule_ledger"][0]["class"], "td_only")
        self.assertEqual(len(verify.only_allowed_deltas(report, set())), 1)

    def test_frozen_reference_matches_the_emitted_index(self):
        inc = os.environ.get("X86_LEGALIZER_TD_INC")
        if not inc or not os.path.exists(inc):
            self.skipTest("set X86_LEGALIZER_TD_INC to X86LegalizerRuleIndex.inc")
        source = testlib.reference_source()
        if source is None:
            self.skipTest("frozen reference revision is not available")
        text, info = source
        result = parse.parse_source(text, info, "C2", CONFIGS["C2"])
        from x86_mir_corpus import emit
        ast = emit.ast_document(result, info)
        with open(inc, "r", encoding="utf-8") as handle:
            td = verify.parse_td_inc(handle.read())
        report = verify.build_report(ast, td)
        classes = {x["rule_set"]: x["class"] for x in report["rule_ledger"]}
        self.assertEqual(classes["X86_EXTRACT"], "td_expands_lambda")
        self.assertEqual(classes["X86_INSERT"], "td_expands_lambda")
        self.assertEqual(report["rule_ledger_totals"],
                         {"chain_rules": 278, "td_num_rules": 282})
        known = {("X86_EXTRACT", "td_expands_lambda"),
                 ("X86_INSERT", "td_expands_lambda")}
        self.assertEqual(verify.only_allowed_deltas(report, known), [])


if __name__ == "__main__":
    unittest.main()
