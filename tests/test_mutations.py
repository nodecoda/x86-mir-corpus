"""Rule mutations are structured from the symbol table's argument roles."""

import os
import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import domain, parse, pin, targets
from x86_mir_corpus import expr as E

MINI = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  const LLT s32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf([](const LegalityQuery &Q) { return true; },
                     changeTo(0, LLT::scalar(64)))
      .narrowScalarIf([](const LegalityQuery &Q) { return true; },
                      LegalizeMutations::changeElementSizeTo(1, 0))
      .bitcastIf([](const LegalityQuery &Q) { return true; },
                 [](const LegalityQuery &Q) {
                   return std::make_pair(0u, LLT::scalar(32));
                 })
      .lowerIf(::any(scalarWiderThan(0, 64), scalarWiderThan(1, 64)), 0)
      .scalarizeIf([](const LegalityQuery &Q) { return true; }, 1)
      .scalarSameSizeAs(0, 1);
}
"""

EMPTY_MUTATION = """\
AArch64LegalizerInfo::AArch64LegalizerInfo(const AArch64Subtarget &ST) {
  getActionDefinitionsBuilder(G_ADD)
      .lowerIf(scalarWiderThan(0, 64), nullptr);
}
"""


def source(text):
    return {"rev": None, "path": "<test>", "sha256": "test",
            "lines": text.count("\n") + 1}


def aarch64(text, config="A2"):
    profile = targets.get("aarch64")
    return parse.parse_source(text, source(text), config,
                              profile.configs[config], profile.rule_set_prefix,
                              profile.llt_extended)


def snapshot_text():
    doc = pin.load(target="aarch64")
    if not doc or not doc.get("snapshot"):
        return None
    path = os.path.join(pin.package_dir(), doc["snapshot"])
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class MutationParseTest(unittest.TestCase):
    def test_call_mutation_keeps_name_slot_and_target(self):
        result = aarch64(MINI)
        rules = result.builders[0].rules
        mutation = rules[0].mutation
        self.assertEqual(mutation["kind"], "call")
        self.assertEqual(mutation["name"], "changeTo")
        self.assertEqual(mutation["type_idx"], 0)
        self.assertEqual(mutation["target"], {"kind": "llt", "llt": "s64"})

    def test_from_slot_target_is_not_mistaken_for_an_llt(self):
        mutation = aarch64(MINI).builders[0].rules[1].mutation
        self.assertEqual(mutation["name"], "changeElementSizeTo")
        self.assertTrue(mutation["qualified"])
        self.assertEqual(mutation["type_idx"], 1)
        self.assertEqual(mutation["target"], {"kind": "slot", "type_idx": 0})

    def test_null_std_function_is_not_type_index_zero(self):
        mutation = aarch64(MINI).builders[0].rules[3].mutation
        self.assertEqual(mutation, {"kind": "none", "name": None,
                                    "type_idx": None, "target": None})
        mutation = aarch64(EMPTY_MUTATION).builders[0].rules[0].mutation
        self.assertEqual(mutation["kind"], "none")

    def test_lambda_mutation_is_recorded_not_unknown(self):
        mutation = aarch64(MINI).builders[0].rules[2].mutation
        self.assertEqual(mutation["kind"], "lambda")
        self.assertIsNone(mutation["type_idx"])

    def test_predicate_slots_come_from_roles(self):
        # lowerIf's scalarWiderThan(1, 64) names slot 1; the call itself has
        # no slot argument, so this can only come from the predicate table.
        self.assertIn(1, aarch64(MINI).builders[0].slots)

    def test_slot_roles_cover_method_specific_slots(self):
        result = aarch64(MINI)
        slots = result.builders[0].slots
        self.assertTrue({0, 1}.issubset(slots))


def apply(name, type_idx, target, types, **env):
    mutation = {"kind": "call", "name": name, "type_idx": type_idx,
                "target": target}
    return domain.apply_mutation(mutation, types, env)


class MutationSemanticsTest(unittest.TestCase):
    """The Python model mirrors LegalizeMutations.cpp at the pinned pin."""

    def test_change_to_type_or_slot(self):
        self.assertEqual(apply("changeTo", 0, {"kind": "llt", "llt": "i64"},
                               [E.scalar(8)]), (0, E.integer(64)))
        self.assertEqual(
            apply("changeTo", 0, {"kind": "slot", "type_idx": 1},
                  [E.scalar(8), E.vec(4, E.scalar(16))]),
            (0, E.vec(4, E.scalar(16))))

    def test_change_element_to_keeps_the_shape(self):
        self.assertEqual(
            apply("changeElementTo", 0, {"kind": "llt", "llt": "f32"},
                  [E.vec(4, E.scalar(32))]),
            (0, E.vec(4, E.float_(32))))
        self.assertEqual(
            apply("changeElementTo", 0, {"kind": "slot", "type_idx": 1},
                  [E.vec(2, E.scalar(32)), E.scalar(16)]),
            (0, E.vec(2, E.scalar(16))))
        # A scalar LLT changes to the new element type wholesale.
        self.assertEqual(
            apply("changeElementTo", 0, {"kind": "llt", "llt": "s16"},
                  [E.scalar(32)]), (0, E.scalar(16)))

    def test_change_element_size_discards_the_element_kind(self):
        self.assertEqual(
            apply("changeElementSizeTo", 0, {"kind": "llt", "llt": "s16"},
                  [E.vec(4, E.integer(32))]),
            (0, E.vec(4, E.integer(16))))
        self.assertEqual(
            apply("changeElementSizeTo", 0, {"kind": "llt", "llt": "s16"},
                  [E.vec(4, E.scalar(32))]),
            (0, E.vec(4, E.scalar(16))))
        self.assertEqual(
            apply("changeElementSizeTo", 0, {"kind": "slot", "type_idx": 1},
                  [E.scalar(8), E.vec(2, E.scalar(64))]),
            (0, E.scalar(64)))
        # changeElementSize asserts on pointers; the query is not well formed.
        self.assertIsNone(
            apply("changeElementSizeTo", 0, {"kind": "llt", "llt": "s16"},
                  [E.ptr(0)]))

    def test_change_element_count_from_slot_or_element_count(self):
        self.assertEqual(
            apply("changeElementCountTo", 0, {"kind": "slot", "type_idx": 1},
                  [E.scalar(32), E.vec(8, E.scalar(16))]),
            (0, E.vec(8, E.scalar(32))))
        self.assertEqual(
            apply("changeElementCountTo", 0, {"kind": "slot", "type_idx": 1},
                  [E.vec(4, E.scalar(16)), E.scalar(32)]),
            (0, E.scalar(16)))
        self.assertEqual(
            apply("changeElementCountTo", 0,
                  {"kind": "element_count", "scalable": False, "count": 2},
                  [E.scalar(32)]), (0, E.vec(2, E.scalar(32))))

    def test_widen_to_the_next_power_of_two_or_multiple(self):
        self.assertEqual(
            apply("widenScalarOrEltToNextPow2", 0,
                  {"kind": "size", "bits": 32}, [E.scalar(8)]),
            (0, E.scalar(32)))
        self.assertEqual(
            apply("widenScalarOrEltToNextPow2", 0,
                  {"kind": "size", "bits": 0}, [E.vec(2, E.scalar(24))]),
            (0, E.vec(2, E.scalar(32))))
        self.assertEqual(
            apply("widenScalarOrEltToNextMultipleOf", 0,
                  {"kind": "size", "bits": 32}, [E.scalar(24)]),
            (0, E.scalar(32)))

    def test_more_elements_and_scalarize(self):
        self.assertEqual(
            apply("moreElementsToNextPow2", 0,
                  {"kind": "size", "bits": 0}, [E.vec(3, E.scalar(32))]),
            (0, E.vec(4, E.scalar(32))))
        self.assertEqual(
            apply("moreElementsToNextPow2", 0,
                  {"kind": "size", "bits": 8}, [E.vec(3, E.scalar(32))]),
            (0, E.vec(8, E.scalar(32))))
        self.assertEqual(
            apply("scalarize", 0, None, [E.vec(4, E.scalar(32))]),
            (0, E.scalar(32)))
        # getNumElements() is fatal for scalable vectors.
        self.assertIsNone(
            apply("moreElementsToNextPow2", 0, None,
                  [E.scalable_vec(4, E.scalar(32))]))
        self.assertIsNone(apply("scalarize", 0, None, [E.scalar(32)]))

    def test_opaque_mutations_are_not_applied(self):
        self.assertIsNone(domain.apply_mutation(
            {"kind": "lambda", "name": None, "type_idx": None,
             "target": None}, [E.scalar(32)]))
        self.assertIsNone(domain.apply_mutation(
            {"kind": "call", "name": "notAMutation", "type_idx": 0,
             "target": None}, [E.scalar(32)]))


class AArch64SnapshotTest(unittest.TestCase):
    def test_every_mutation_role_is_structured(self):
        text = snapshot_text()
        if text is None:
            self.skipTest("AArch64 snapshot not bundled")
        profile = targets.get("aarch64")
        result = parse.parse_source(
            text, source(text), "A2", profile.configs["A2"],
            profile.rule_set_prefix, profile.llt_extended)
        mutations = [r.mutation for b in result.builders for r in b.rules
                     if r.mutation is not None]
        kinds = [m["kind"] for m in mutations]
        self.assertEqual(kinds.count("unknown"), 0)
        self.assertEqual(kinds.count("call"), 23)
        self.assertEqual(kinds.count("lambda"), 8)
        self.assertEqual(kinds.count("none"), 1)
        self.assertEqual((result.unknown_methods, result.unknown_predicates,
                          result.unknown_refs), ([], [], []))


if __name__ == "__main__":
    unittest.main()
