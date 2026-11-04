"""Finite-quotient analysis over a synthetic legalizer constructor."""

import unittest

from x86_mir_corpus import domain, parse, quotient, targets
from x86_mir_corpus import expr as E

PROFILE = targets.get("x86")


def analyze(source, config="C2"):
    result = parse.parse_source(
        source, {"rev": None, "path": "<test>"}, config,
        PROFILE.configs[config], PROFILE.rule_set_prefix,
        PROFILE.llt_extended)
    return result, quotient.analyze(result, PROFILE, config)


THREE_PREDICATES = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s16 = LLT::scalar(16);
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf(typeIs(0, s8), widenScalarToNextPow2(0))
      .widenScalarIf(typeIs(0, s16), widenScalarToNextPow2(0))
      .widenScalarIf(typeIs(0, s32), widenScalarToNextPow2(0));
}
"""

THRESHOLD = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s16 = LLT::scalar(16);
  const LLT s32 = LLT::scalar(32);
  const LLT s64 = LLT::scalar(64);
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf(scalarNarrowerThan(0, 32), widenScalarToNextPow2(0));
  getActionDefinitionsBuilder(G_SUB)
      .widenScalarIf(typeIs(0, s64), widenScalarToNextPow2(0));
}
"""

PAIR = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s16 = LLT::scalar(16);
  const LLT s32 = LLT::scalar(32);
  const LLT p0 = LLT::pointer(0);
  getActionDefinitionsBuilder(G_SDIV)
      .widenScalarIf(typePairInSet(0, 1, {{s32, s16}}),
                     widenScalarToNextPow2(0));
  getActionDefinitionsBuilder(G_LOAD)
      .legalForTypesWithMemDesc({{s8, p0, s8, 8}});
}
"""


class AxisTest(unittest.TestCase):
    def test_three_type_predicates_partition_three_classes(self):
        _, report = analyze(THREE_PREDICATES)
        axis = report["per_builder"]["X86_ADD"]["slot0"]
        # p0 is a fourth class: it matches no typeIs predicate.
        self.assertEqual(axis["keys"], 4)
        self.assertEqual(axis["length"], 4)
        self.assertTrue({("s8",), ("s16",), ("s32",)}
                        <= {tuple(rep) for rep in axis["representatives"]})

    def test_threshold_boundary_splits_the_class_axis(self):
        _, report = analyze(THRESHOLD)
        axis = report["per_builder"]["X86_ADD"]["slot0"]
        self.assertEqual(axis["keys"], 2)
        kinds = {next(iter(rep)) for rep in axis["representatives"]}
        self.assertEqual(kinds, {"p0", "s8"})

    def test_pair_predicate_creates_one_two_slot_axis(self):
        _, report = analyze(PAIR)
        axes = report["per_builder"]["X86_SDIV"]
        self.assertIn("pair(0,1)", axes)
        self.assertNotIn("slot0", axes)
        self.assertNotIn("slot1", axes)
        self.assertEqual(axes["pair(0,1)"]["length"], 9)
        self.assertEqual(axes["pair(0,1)"]["keys"], 2)

    def test_unconditional_list_rule_still_creates_a_read_axis(self):
        source = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s32 = LLT::scalar(32);
  const LLT s64 = LLT::scalar(64);
  getActionDefinitionsBuilder(G_ADD).legalFor({{s32}, {s64}});
}
"""
        _, report = analyze(source)
        axis = report["per_builder"]["X86_ADD"]["slot0"]
        self.assertEqual(axis["keys"], 2)


class ReportTest(unittest.TestCase):
    def test_report_shape_and_notes(self):
        _, report = analyze(THREE_PREDICATES)
        self.assertEqual(report["schema"], "llvm-legalizer-quotient-report")
        self.assertEqual(report["target"], "x86")
        self.assertEqual(report["config"], "C2")
        self.assertIn("slot0", report["axes"])
        self.assertIn("config", report["axes"])
        self.assertIn("opcode", report["axes"])
        self.assertTrue(report["notes"])
        self.assertEqual(report["coverage"]["rules_total"], 3)
        self.assertEqual(report["coverage"]["rules_with_representative"], 3)

    def test_representatives_are_class_names(self):
        _, report = analyze(THREE_PREDICATES)
        for axis in report["axes"].values():
            for rep in axis["representatives"]:
                self.assertTrue(all(isinstance(item, str) for item in rep))

    def test_opaque_lambda_is_reported_not_guessed(self):
        source = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD).legalIf([](const LegalityQuery &Q) {
    return Q.Types[0].isScalar();
  });
}
"""
        _, report = analyze(source)
        self.assertEqual(report["opaque_builders"], ["X86_ADD"])

    def test_axis_name_variants(self):
        self.assertEqual(quotient.axis_name((0,)), "slot0")
        self.assertEqual(quotient.axis_name((0, 1)), "pair(0,1)")
        self.assertEqual(quotient.axis_name((0, 1, 2)), "triple(0,1,2)")
        self.assertEqual(quotient.axis_name((0, "mem")), "slot0+mem")

    def test_query_keeps_unrelated_slots_at_the_representative(self):
        classes = domain.sorted_llts({E.scalar(8), E.scalar(32)})
        types, _, _ = quotient._query((0,), (classes[1],), classes, (), (), 2)
        self.assertEqual(types[0], classes[1])
        self.assertEqual(types[1], classes[0])


if __name__ == "__main__":
    unittest.main()
