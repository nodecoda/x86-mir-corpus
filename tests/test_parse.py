import unittest

import testlib
from x86_mir_corpus import expr as E


MINI = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s32 = LLT::scalar(32);
  const LLT p0 = LLT::pointer(0);

  for (unsigned Op : {G_ADD, G_SUB}) {
    getActionDefinitionsBuilder(Op)
        .legalFor({{s32, s32}})
        .widenScalarToNextPow2(0);
  }

  auto &Action = getActionDefinitionsBuilder(G_LOAD);
  if (Subtarget.hasAVX2())
    Action.legalForTypesWithMemDesc({{s32, p0, s8, 8}});
  else
    Action.legalForTypesWithMemDesc({{s8, p0, s8, 8}});

  getActionDefinitionsBuilder(G_CTPOP)
      .legalIf([](unsigned TypeIdx, LLT Ty, const LegalityQuery &Q) {
        return typeIs(0, s32);
      })
      .lower();
}
"""


def builder(result, name):
    for b in result.builders:
        if b.name == name:
            return b
    raise AssertionError(f"no builder {name}: {[b.name for b in result.builders]}")


class ParseTest(unittest.TestCase):
    def test_census_of_the_mini_source(self):
        r = testlib.parse_text(MINI)
        self.assertEqual(r.census(), {
            "call_sites": 3, "builders": 4, "opcodes": 4,
            "classes": 3, "mem_classes": 1, "aligns": 1,
        })
        self.assertEqual(r.unknown_methods, [])
        self.assertEqual(r.unknown_refs, [])

    def test_for_loop_expands_one_builder_per_opcode(self):
        r = testlib.parse_text(MINI)
        self.assertEqual([b.name for b in r.builders],
                         ["X86_ADD", "X86_SUB", "X86_LOAD", "X86_CTPOP"])
        self.assertEqual(builder(r, "X86_ADD").opcodes, ["G_ADD"])
        self.assertEqual(builder(r, "X86_SUB").opcodes, ["G_SUB"])

    def test_auto_builder_takes_the_configured_branch(self):
        c2 = builder(testlib.parse_text(MINI, "C2"), "X86_LOAD")
        self.assertTrue(c2.has_mem)
        self.assertEqual(c2.slots, {0, 1})
        self.assertEqual([E.llt_name(t) for t in c2.mem_classes], ["s8"])
        self.assertEqual(c2.aligns, {8})
        self.assertEqual(len(c2.rules), 1)
        self.assertEqual(c2.rules[0].guard[0][1], True)

        c3 = builder(testlib.parse_text(MINI, "C3"), "X86_LOAD")
        self.assertEqual(len(c3.rules), 1)
        # C3 has no AVX2, so the else branch is the active one.
        self.assertEqual(E.llt_name(list(c3.mem_classes)[0]), "s8")
        self.assertEqual(c3.rules[0].guard[0][1], True)
        self.assertTrue(c3.rules[0].guard[0][0].startswith("!("))

    def test_opaque_lambda_is_counted_not_expanded(self):
        r = testlib.parse_text(MINI)
        self.assertEqual(r.opaque_lambdas, 1)
        ctpop = builder(r, "X86_CTPOP")
        self.assertEqual([x.method for x in ctpop.rules], ["legalIf", "lower"])

    def test_unknown_method_is_recorded_with_its_line(self):
        src = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  getActionDefinitionsBuilder(G_ADD)
      .frobnicate({s8});
}
"""
        r = testlib.parse_text(src)
        self.assertEqual(r.unknown_methods, [("frobnicate", 4)])

    def test_unevaluable_declaration_is_recorded_with_its_line(self):
        src = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT weird =
      LLT::scalar(Subtarget.hasFancy() ? 8 : 16);
  const LLT s8 = LLT::scalar(8);
  getActionDefinitionsBuilder(G_ADD).legalFor({{s8, s8}});
}
"""
        r = testlib.parse_text(src)
        self.assertEqual(r.unknown_refs[0][0], "weird")
        self.assertEqual(r.unknown_refs[0][1], 2)
        self.assertIn("hasFancy", r.unknown_refs[0][2])

    def test_cartesian_product_has_one_slot_per_list(self):
        src = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s32 = LLT::scalar(32);
  const LLT p0 = LLT::pointer(0);
  getActionDefinitionsBuilder(G_PTRTOINT)
      .legalForCartesianProduct({s32, p0}, {p0})
      .maxScalar(0, s32);
}
"""
        r = testlib.parse_text(src)
        ptrtoint = builder(r, "X86_PTRTOINT")
        self.assertEqual(ptrtoint.slots, {0, 1})
        self.assertEqual(ptrtoint.num_slots, 2)

    def test_bad_opcode_argument_reports_the_builder_line(self):
        src = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  getActionDefinitionsBuilder(NotAnOpcode)
      .legalFor({{LLT::scalar(8), LLT::scalar(8)}});
}
"""
        with self.assertRaises(E.ExprError) as ctx:
            testlib.parse_text(src)
        self.assertIn("line 2", str(ctx.exception))
        self.assertIn("NotAnOpcode", str(ctx.exception))

    def test_upstream_main_census_and_ctpop_delta(self):
        source = testlib.source_at("upstream/main")
        if source is None:
            self.skipTest("upstream/main is not available in this checkout")
        text, info = source
        r = testlib.parse_text(text, "C2")
        self.assertEqual(r.census(), {
            "call_sites": 58, "builders": 62, "opcodes": 115,
            "classes": 24, "mem_classes": 23, "aligns": 1,
        })
        self.assertEqual(r.unknown_methods, [])
        self.assertEqual(r.unknown_refs, [])
        self.assertEqual(r.opaque_lambdas, 10)
        self.assertEqual(sum(len(b.rules) for b in r.builders), 275)

        # The frozen reference adds the two HasVPOPCNTDQ legalFor rules that
        # upstream/main does not have yet.  Keeping this assertion here makes
        # the source delta visible instead of silently filling upstream shapes
        # with frozen-revision answers.
        reference = testlib.reference_source()
        if reference is None:
            self.skipTest("frozen reference revision is not available")
        ref = testlib.parse_text(reference[0], "C2")
        up_ctpop = builder(r, "X86_CTPOP")
        ref_ctpop = builder(ref, "X86_CTPOP")
        self.assertEqual(len(up_ctpop.rules), 5)
        self.assertEqual(len(ref_ctpop.rules), 7)
        up_args = {x.arg_text[0] for x in up_ctpop.rules}
        ref_only = [x for x in ref_ctpop.rules
                    if x.arg_text[0] not in up_args]
        self.assertEqual(len(ref_only), 2)
        self.assertTrue(all("VPOPCNTDQ" in x.arg_text[0] for x in ref_only))

    def test_frozen_reference_census(self):
        source = testlib.reference_source()
        if source is None:
            self.skipTest("frozen reference revision is not available")
        text, info = source
        r = testlib.parse_text(text, "C2")
        self.assertEqual(info["rev"], "ebac17721d12")
        self.assertEqual(r.census(), {
            "call_sites": 58, "builders": 62, "opcodes": 115,
            "classes": 24, "mem_classes": 23, "aligns": 1,
        })
        self.assertEqual(r.unknown_methods, [])
        self.assertEqual(r.unknown_refs, [])
        self.assertEqual(r.opaque_lambdas, 10)
        self.assertEqual(sum(len(b.rules) for b in r.builders), 277)
        multi = {b.name: b.opcodes for b in r.builders if len(b.opcodes) > 1}
        self.assertEqual(multi["X86_EXTRACT"], ["G_EXTRACT", "G_INSERT"])


if __name__ == "__main__":
    unittest.main()
