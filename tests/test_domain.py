import os
import re
import tempfile
import unittest

import testlib
from x86_mir_corpus import domain, emit, parse
from x86_mir_corpus import expr as E
from x86_mir_corpus.target import CONFIGS

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


PRUNE = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  const LLT s32 = LLT::scalar(32);
  const LLT p0 = LLT::pointer(0);
  getActionDefinitionsBuilder(G_CTLZ)
      .legalFor({s32})
      .scalarSameSizeAs(0, 1);
}
"""


def builder(result, name):
    return next(b for b in result.builders if b.name == name)


def mini_result(config="C2", text=MINI):
    source = {"rev": None, "path": "<test>", "sha256": "test", "lines": 1}
    return parse.parse_source(text, source, config, CONFIGS[config])


class DomainTest(unittest.TestCase):
    def test_witness_points_are_shape_only(self):
        result = mini_result()
        points, _, _, panel, pruned, _ = domain.build_points(result, "witness")
        self.assertEqual(pruned, 0)
        self.assertEqual(len(points), 75)
        self.assertEqual(panel, domain.BOUNDARY_PANEL)
        for p in points:
            self.assertNotIn("expected", p)
            self.assertTrue(p["id"].startswith("cpp/"))
            self.assertGreaterEqual(len(p["types"]), 1)
        self.assertEqual(len({p["id"] for p in points}), len(points))

    def test_origin_counts(self):
        points, _, _, _, _, _ = domain.build_points(mini_result(), "witness")
        counts = {}
        for p in points:
            counts[p["origin"]] = counts.get(p["origin"], 0) + 1
        self.assertEqual(counts, {"as": 20, "boundary": 48, "rule": 7})

    def test_address_space_points_cover_memory_pointer(self):
        points, _, _, _, _, _ = domain.build_points(mini_result(), "witness")
        load_as = [p for p in points
                   if p["rule_set"] == "X86_LOAD" and p["origin"] == "as"]
        self.assertEqual(len(load_as), 10)
        self.assertIn((["p1", "p0"], "p0"), [(p["types"], p["mem"]) for p in load_as])
        self.assertIn((["p0", "p1"], "p0"), [(p["types"], p["mem"]) for p in load_as])

    def test_rule_points_carry_the_rule_index(self):
        points, _, _, _, _, _ = domain.build_points(mini_result(), "witness")
        load = [p for p in points
                if p["rule_set"] == "X86_LOAD" and p["origin"] == "rule"]
        self.assertEqual([p["rule_index"] for p in load], [0])
        self.assertEqual(load[0]["mem"], "s8")

    def test_full_and_deep_add_class_pairs(self):
        witness, _, _, _, _, _ = domain.build_points(mini_result(), "witness")
        full, _, _, _, _, _ = domain.build_points(mini_result(), "full")
        deep, _, _, _, _, _ = domain.build_points(mini_result(), "deep")
        self.assertGreater(len(full), len(witness))
        self.assertGreater(len(deep), len(witness))
        self.assertTrue(any(p["origin"] == "classpair" for p in full))
        self.assertTrue(any(p["origin"] == "classpair" for p in deep))

    def test_pointer_in_mutated_slot_is_pruned(self):
        full, _, _, _, pruned, _ = domain.build_points(
            mini_result(text=PRUNE), "full")
        self.assertGreater(pruned, 0)
        ctlz = [p for p in full
                if p["rule_set"] == "X86_CTLZ" and p["origin"] == "classpair"]
        self.assertTrue(ctlz)
        # scalarSameSizeAs(0, 1) mutates slot 0, so a pointer there is not a
        # well-formed query; a pointer in slot 1 is fine.
        self.assertFalse(any(p["types"][0] == "p0" for p in ctlz))
        self.assertTrue(any(p["types"][1] == "p0" for p in ctlz))

    def test_points_are_byte_deterministic(self):
        result = mini_result()
        with tempfile.TemporaryDirectory() as tmp:
            docs = []
            for name in ("a.json", "b.json"):
                points, classes, mem_classes, panel, pruned, _ = domain.build_points(
                    result, "witness")
                doc = emit.points_document(result, result.source, "witness",
                                           points, classes, mem_classes, panel,
                                           pruned)
                path = os.path.join(tmp, name)
                emit.write_json(path, doc)
                with open(path, "rb") as handle:
                    docs.append(handle.read())
        self.assertEqual(docs[0], docs[1])


FEATURE_CLAMP = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s8 = LLT::scalar(8);
  getActionDefinitionsBuilder(G_ADD)
      .clampMaxNumElements(0, s8, Subtarget.hasBWI() ? 64 : 16)
      .scalarize(0);
}
"""

INACTIVE = """\
X86LegalizerInfo::X86LegalizerInfo(const X86Subtarget &STI) : Subtarget(STI) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor(Subtarget.hasAVX2(), {s32})
      .lower();
}
"""


def lanes(name):
    if not name.startswith("v"):
        return 1
    return int(name[1:].split("s", 1)[0].rstrip("p0") or 1)


class CoverageTest(unittest.TestCase):
    def test_feature_dependent_argument_is_evaluated_at_parse_time(self):
        c2 = mini_result(text=FEATURE_CLAMP)
        rule = builder(c2, "X86_ADD").rules[0]
        self.assertEqual(rule.evaluated_args, [0, E.scalar(8), 64])
        c0 = mini_result("C0", FEATURE_CLAMP)
        self.assertEqual(builder(c0, "X86_ADD").rules[0].evaluated_args,
                         [0, E.scalar(8), 16])

    def test_feature_dependent_clamp_gets_a_first_match_probe(self):
        result = mini_result(text=FEATURE_CLAMP)
        points, _, _, _, _, coverage = domain.build_points(result, "witness")
        add = [p for p in points
               if p["rule_set"] == "X86_ADD" and p["origin"] == "rule"
               and p["rule_index"] == 0]
        self.assertTrue(add)
        self.assertEqual({lanes(p["types"][0]) for p in add}, {65})
        self.assertEqual(coverage["rules"]["unprobed"], [])

    def test_feature_gated_rule_is_reported_inactive(self):
        c0 = mini_result("C0", INACTIVE)
        _, _, _, _, _, coverage = domain.build_points(c0, "witness")
        self.assertEqual(coverage["rules"]["source_total"], 2)
        self.assertEqual(coverage["rules"]["active"], 1)
        self.assertEqual(coverage["rules"]["inactive"][0]["rule_index"], 0)
        c2 = mini_result("C2", INACTIVE)
        _, _, _, _, _, c2_coverage = domain.build_points(c2, "witness")
        self.assertEqual(c2_coverage["rules"]["active"], 2)

    def test_multi_opcode_builder_gets_every_opcode_covered(self):
        source = testlib.reference_source()
        if source is None:
            self.skipTest("frozen reference revision is not available")
        result = testlib.parse_text(source[0], "C2")
        points, _, _, _, _, coverage = domain.build_points(
            result, "witness", domain.load_coverage_exceptions())
        self.assertEqual(coverage["opcodes"]["uncovered"], [])
        shape_atoms = coverage["opcodes"]["per_opcode_shape_atoms"]
        for name, counts in shape_atoms.items():
            for opcode, count in counts.items():
                self.assertGreater(count, 0, f"{name}:{opcode}")
        for opcode in ("G_EXTRACT", "G_INSERT"):
            atoms = [p for p in points
                     if p["rule_set"] == "X86_EXTRACT" and p["opcode"] == opcode]
            self.assertTrue(atoms, opcode)
            self.assertTrue(any(p["origin"] == "rule" for p in atoms), opcode)
            self.assertTrue(any(p["origin"] == "boundary" for p in atoms), opcode)
            self.assertTrue(any(p["origin"] == "as" for p in atoms), opcode)


LLT_GRAMMAR = re.compile(r"^(?:p\d+|s\d+|v\d+s\d+|v\d+p\d+)$")


class UpstreamShapeTest(unittest.TestCase):
    def test_upstream_types_follow_the_llt_grammar(self):
        source = testlib.source_at("upstream/main")
        if source is None:
            self.skipTest("upstream/main is not available")
        result = testlib.parse_text(source[0], "C2")
        points, _, _, _, _, _ = domain.build_points(
            result, "witness", domain.load_coverage_exceptions())
        self.assertTrue(points)
        for p in points:
            for ty in p["types"]:
                self.assertRegex(ty, LLT_GRAMMAR)
            if p["mem"] is not None:
                self.assertRegex(p["mem"], LLT_GRAMMAR)


if __name__ == "__main__":
    unittest.main()
