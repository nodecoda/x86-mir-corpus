"""Target profiles: x86 compatibility, AArch64 vocabulary, early returns."""

import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import parse, pin, targets

MINI_A64 = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  const LLT s32 = LLT::scalar(32);
  if (!ST.hasNEON() || !ST.hasFPARMv8())
    return;
  getActionDefinitionsBuilder(G_ADD).legalFor({s32});
}
"""

CODE_MODEL = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  const LLT p0 = LLT::pointer(0, 64);
  if (TM.getCodeModel() == CodeModel::Small)
    getActionDefinitionsBuilder(G_GLOBAL_VALUE).custom();
  else
    getActionDefinitionsBuilder(G_GLOBAL_VALUE).legalFor({p0});
}
"""


def source():
    return {"rev": None, "path": "<test>", "sha256": "test", "lines": 1}


class ProfileTest(unittest.TestCase):
    def test_x86_profile_is_unchanged(self):
        x86 = targets.get("x86")
        self.assertEqual(x86.source_path,
                         "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp")
        self.assertEqual(x86.rule_set_prefix, "X86_")
        self.assertEqual(x86.default_config, "C2")
        self.assertEqual(sorted(x86.configs), ["C0", "C1", "C2", "C3"])
        self.assertFalse(x86.llt_extended)

    def test_aarch64_profile(self):
        a64 = targets.get("aarch64")
        self.assertEqual(a64.rule_set_prefix, "AArch64_")
        self.assertEqual(a64.default_config, "A2")
        self.assertEqual(sorted(a64.configs), ["A0", "A1", "A2", "A3"])
        self.assertTrue(a64.llt_extended)
        self.assertFalse(a64.configs["A0"]["hasFullFP16"])
        self.assertFalse(a64.configs["A1"]["hasSVE"])
        self.assertTrue(a64.configs["A2"]["hasSVE"])
        self.assertEqual(a64.pointer_bits("A2"), 64)
        # Integer and float kinds must be part of the reviewed panel.
        self.assertIn(("i", 32), a64.boundary_panel)
        self.assertIn(("f", 16, "ieee"), a64.boundary_panel)
        self.assertIn(("nxv", 4, ("i", 32)), a64.boundary_panel)

    def test_aarch64_source_is_pinned(self):
        self.assertEqual(pin.revision(target="aarch64"),
                         "fed20e0e68323be32cb0fe8af679b8cb34e72112")


class AArch64ParseTest(unittest.TestCase):
    def test_early_return_disables_the_rest_of_the_constructor(self):
        a64 = targets.get("aarch64")
        off = dict(a64.configs["A2"], hasNEON=False)
        result = parse.parse_source(MINI_A64, source(), "A2", off,
                                    "AArch64_", True)
        self.assertEqual(result.builders, [])

    def test_constructor_body_parses_with_features_on(self):
        a64 = targets.get("aarch64")
        result = parse.parse_source(MINI_A64, source(), "A2",
                                    a64.configs["A2"], "AArch64_", True)
        self.assertEqual(len(result.builders), 1)
        self.assertEqual(result.builders[0].name, "AArch64_ADD")
        self.assertEqual(result.unknown_refs, [])

    def test_code_model_guard_prunes_one_branch(self):
        a64 = targets.get("aarch64")
        result = parse.parse_source(CODE_MODEL, source(), "A2",
                                    a64.configs["A2"], "AArch64_", True)
        self.assertEqual([b.name for b in result.builders],
                         ["AArch64_GLOBAL_VALUE"])
        self.assertEqual(result.builders[0].rules[0].method, "custom")


if __name__ == "__main__":
    unittest.main()
