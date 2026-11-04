"""LLT type-model tests: kinds, spellings, sizes, and predicate semantics."""

import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import domain, lexer, triage
from x86_mir_corpus import expr as E


def eval_src(src, env=None):
    node = E.Parser(lexer.lex(src)).parse_expr()
    return E.eval_expr(node, env or {"__llt_extended__": True})


def pred(src, types, mem=None, align=0, env=None):
    node = E.Parser(lexer.lex(src)).parse_expr()
    base = {
        "s8": E.scalar(8), "s16": E.scalar(16), "s32": E.scalar(32),
        "s64": E.scalar(64), "i8": E.integer(8), "i32": E.integer(32),
        "f16": E.float_(16), "f32": E.float_(32), "bf16": E.float_(16, "bfloat"),
        "p0": E.ptr(0), "v4s32": E.vec(4, E.scalar(32)),
        "v4i16": E.vec(4, E.integer(16)), "nxv4s32": E.scalable_vec(4, E.scalar(32)),
        "__pointer_bits__": 64, "__llt_extended__": True,
    }
    base.update(env or {})
    return domain._pred_match(node, types, mem, align, base)


class LLTModelTest(unittest.TestCase):
    def test_extended_kinds_are_distinct(self):
        self.assertNotEqual(E.scalar(16), E.float_(16))
        self.assertNotEqual(E.scalar(16), E.integer(16))
        self.assertNotEqual(E.float_(16), E.float_(16, "bfloat"))
        self.assertNotEqual(E.float_(128), E.float_(128, "ppc"))
        self.assertNotEqual(E.vec(4, E.scalar(32)),
                            E.scalable_vec(4, E.scalar(32)))
        self.assertNotEqual(E.vec(4, E.scalar(32)), E.vec(4, E.integer(32)))

    def test_scalar_predicate_excludes_pointers(self):
        self.assertTrue(E.is_scalar(E.scalar(32)))
        self.assertTrue(E.is_scalar(E.integer(32)))
        self.assertTrue(E.is_scalar(E.float_(32)))
        self.assertFalse(E.is_scalar(E.ptr(0)))
        self.assertFalse(E.is_scalar(E.vec(2, E.scalar(32))))
        self.assertTrue(E.is_vec(E.vec(2, E.scalar(32))))
        self.assertTrue(E.is_vec(E.scalable_vec(2, E.scalar(32))))
        self.assertTrue(E.is_scalable(E.scalable_vec(2, E.scalar(32))))

    def test_names_and_sizes(self):
        self.assertEqual(E.llt_name(E.integer(16)), "i16")
        self.assertEqual(E.llt_name(E.float_(16)), "f16")
        self.assertEqual(E.llt_name(E.float_(16, "bfloat")), "bf16")
        self.assertEqual(E.llt_name(E.float_(80, "x86")), "f80")
        self.assertEqual(E.llt_name(E.float_(128, "ppc")), "ppcf128")
        self.assertEqual(E.llt_name(E.scalable_vec(4, E.float_(32))), "nxv4f32")
        self.assertEqual(E.llt_bits(E.scalable_vec(4, E.integer(32))), 128)
        self.assertEqual(E.llt_lanes(E.scalable_vec(4, E.scalar(8))), 4)
        self.assertEqual(E.llt_scalar_bits(E.ptr(0), ptr_bits=64), 64)

    def test_constructor_evaluation_honours_extended_llt(self):
        extended = {"__llt_extended__": True}
        compact = {"__llt_extended__": False}
        self.assertEqual(eval_src("LLT::integer(16)", extended), E.integer(16))
        self.assertEqual(eval_src("LLT::integer(16)", compact), E.scalar(16))
        self.assertEqual(eval_src("LLT::float16()", extended), E.float_(16))
        self.assertEqual(eval_src("LLT::float16()", compact), E.scalar(16))
        # bfloat16/x86fp80/ppcf128 are FLOAT regardless of extended LLT.
        self.assertEqual(eval_src("LLT::bfloat16()", compact),
                         E.float_(16, "bfloat"))
        self.assertEqual(eval_src("LLT::fixed_vector(8, LLT::integer(16))",
                                  extended), E.vec(8, E.integer(16)))
        self.assertEqual(eval_src("LLT::scalable_vector(4, LLT::float32())",
                                  extended), E.scalable_vec(4, E.float_(32)))
        self.assertEqual(eval_src("LLT::vector(ElementCount::getScalable(4),"
                                  " LLT::integer(32))", extended),
                         E.scalable_vec(4, E.integer(32)))


class PredicateSemanticsTest(unittest.TestCase):
    def test_scalar_predicates_accept_float_and_integer(self):
        self.assertTrue(pred("isScalar(0)", [E.float_(16)]))
        self.assertTrue(pred("isScalar(0)", [E.integer(16)]))
        self.assertTrue(pred("isScalar(0)", [E.scalar(16)]))
        self.assertFalse(pred("isScalar(0)", [E.ptr(0)]))
        self.assertTrue(pred("scalarNarrowerThan(0, 32)", [E.float_(16)]))
        self.assertFalse(pred("scalarNarrowerThan(0, 32)", [E.ptr(0)]))

    def test_vector_predicates_distinguish_fixed_and_scalable(self):
        fixed = E.vec(4, E.scalar(32))
        scalable = E.scalable_vec(4, E.scalar(32))
        self.assertTrue(pred("isVector(0)", [scalable]))
        self.assertTrue(pred("isVector(0)", [fixed]))
        self.assertTrue(pred("elementTypeIs(0, s32)", [scalable]))
        self.assertTrue(pred("elementTypeIs(0, s32)", [fixed]))
        self.assertFalse(pred("elementTypeIs(0, s32)", [E.vec(4, E.integer(16))]))
        self.assertFalse(pred("numElementsNotPow2(0)", [scalable]))
        self.assertFalse(pred("vectorElementCountIsGreaterThan(0, 2)",
                              [scalable]))

    def test_size_comparisons_stay_undecided_for_scalable(self):
        self.assertIsNone(pred("sameSize(0, 1)",
                               [E.scalable_vec(4, E.scalar(32)),
                                E.vec(4, E.scalar(32))]))
        self.assertIsNone(pred("sizeIs(0, 128)",
                               [E.scalable_vec(4, E.scalar(32))]))

    def test_pointer_relations(self):
        self.assertTrue(pred("isPointer(0, 0)", [E.ptr(0)]))
        self.assertFalse(pred("isPointer(0, 1)", [E.ptr(0)]))
        self.assertTrue(pred("isPointerVector(0)",
                             [E.vec(2, E.ptr(0))]))
        self.assertFalse(pred("isPointerVector(0)",
                              [E.scalable_vec(2, E.scalar(64))]))
        self.assertTrue(pred("sameSize(0, 1)", [E.ptr(0), E.scalar(64)]))

    def test_combinators(self):
        self.assertTrue(pred("all(typeInSet(0, {s16, f16}), isScalar(0))",
                             [E.float_(16)]))
        self.assertFalse(pred("all(typeInSet(0, {s16, f16}), isScalar(0))",
                              [E.ptr(0)]))
        self.assertTrue(pred("any(scalarWiderThan(0, 64), isVector(1))",
                             [E.scalar(8), E.vec(2, E.scalar(8))]))
        self.assertIsNone(pred("predNot(immIs(0, 3))", [E.scalar(8)]))


class CorpusTypeNameTest(unittest.TestCase):
    def test_norm_type_round_trips_all_kinds(self):
        cases = {
            "v2s32:64": "v2s32", "v2p0:64": "v2p0", "nxv8i16": "nxv8i16",
            "v4bf16": "v4bf16", "f32": "f32", "bf16": "bf16",
            "ppcf128": "ppcf128", "p0:64": "p0", "v4f16": "v4f16",
            "nxv2p0:64": "nxv2p0", "i128": "i128",
        }
        for text, expected in cases.items():
            self.assertEqual(triage.norm_type(text), expected)


if __name__ == "__main__":
    unittest.main()
