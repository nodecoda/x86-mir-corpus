"""The runtime half of the DSL symbol table must match the checked-in table."""

import unittest

from x86_mir_corpus import domain, dsl
from x86_mir_corpus import expr as E
from x86_mir_corpus.lexer import lex


def evaluate(source, types=(), **env):
    toks = lex(source)
    node = E.Parser(toks, 0, len(toks)).parse_expr()
    return E.eval_expr(node, dict(env, __types__=list(types)))


class DispatchEqualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = dsl.load()

    def test_every_exact_llt_query_has_an_implementation(self):
        expected = dsl.llt_query_names(self.table, resolution="exact")
        self.assertEqual(expected, set(E.LLT_QUERY_IMPL))

    def test_every_exact_element_count_op_has_an_implementation(self):
        expected = {entry["name"] for entry in self.table["element_count"]
                    if entry["resolution"] == "exact"}
        self.assertEqual(expected, set(E.ELEMENT_COUNT_IMPL))

    def test_every_syntax_llt_constructor_has_an_implementation(self):
        expected = {entry["name"] for entry in self.table["llt_constructors"]
                    if entry["resolution"] == "exact"}
        self.assertEqual(expected, set(E.LLT_CTOR_IMPL))

    def test_every_exact_predicate_has_an_implementation(self):
        expected = {entry["name"] for entry in self.table["predicates"]
                    if entry["domain"] == "exact"}
        self.assertEqual(expected, set(domain.PREDICATE_IMPL))

    def test_every_mutation_factory_has_an_implementation(self):
        self.assertEqual(dsl.mutation_names(self.table),
                         set(domain.MUTATION_IMPL))


class LltModelTest(unittest.TestCase):
    def test_scalar_queries(self):
        s16 = E.scalar(16)
        self.assertTrue(evaluate("T.isScalar()", T=s16))
        self.assertFalse(evaluate("T.isVector()", T=s16))
        self.assertTrue(evaluate("T.isAnyScalar()", T=s16))
        self.assertFalse(evaluate("T.isInteger()", T=s16))
        self.assertEqual(evaluate("T.getSizeInBits()", T=s16), 16)
        self.assertEqual(evaluate("T.getSizeInBytes()", T=s16), 2)

    def test_pointer_queries_use_the_data_layout(self):
        p0 = E.ptr(0)
        env = dict(__pointer_bits__=64)
        self.assertTrue(evaluate("T.isPointer()", T=p0, **env))
        self.assertEqual(evaluate("T.getSizeInBits()", T=p0, **env), 64)
        self.assertEqual(evaluate("T.getScalarSizeInBits()", T=p0, **env), 64)
        self.assertEqual(evaluate("T.getAddressSpace()", T=p0, **env), 0)

    def test_chained_query_receiver_is_preserved(self):
        types = [E.scalar(8), E.float_(16), E.vec(4, E.scalar(32))]
        self.assertFalse(
            evaluate("Query.Types[0].getScalarType().isFloat16()",
                     types=types, __llt_extended__=True))
        self.assertTrue(
            evaluate("Query.Types[1].getScalarType().isFloat16()",
                     types=types, __llt_extended__=True))
        self.assertEqual(
            evaluate("Query.Types[2].getNumElements()", types=types), 4)
        self.assertEqual(
            evaluate("Query.Types[2].divide(2)", types=types),
            E.vec(2, E.scalar(32)))

    def test_extended_llt_changes_is_float16(self):
        s16 = E.scalar(16)
        self.assertFalse(evaluate("T.isFloat16()", T=s16,
                                  __llt_extended__=True))
        self.assertTrue(evaluate("T.isFloat16()", T=s16,
                                 __llt_extended__=False))

    def test_scalable_vector_queries(self):
        nxv = E.scalable_vec(4, E.scalar(32))
        self.assertTrue(evaluate("T.isVector()", T=nxv))
        self.assertTrue(evaluate("T.isScalableVector()", T=nxv))
        self.assertFalse(evaluate("T.isFixedVector()", T=nxv))
        self.assertEqual(evaluate("T.getSizeInBits()", T=nxv), 128)
        self.assertEqual(evaluate("T.getElementCount()", T=nxv),
                         E.element_count("scalable", 4))

    def test_mutations(self):
        v = E.vec(4, E.scalar(16))
        self.assertEqual(evaluate("T.changeElementSize(32)", T=v),
                         E.vec(4, E.scalar(32)))
        self.assertEqual(evaluate("T.changeVectorElementType(i8)", T=v,
                                  i8=E.integer(8)),
                         E.vec(4, E.integer(8)))
        self.assertEqual(evaluate("T.multiplyElements(2)", T=v),
                         E.vec(8, E.scalar(16)))
        self.assertEqual(evaluate("T.changeToInteger()", T=v),
                         E.vec(4, E.integer(16)))

    def test_unknown_query_reports_a_table_gap(self):
        with self.assertRaises(E.EvalError):
            evaluate("T.notARealQuery()", T=E.scalar(32))


def match(source, types=(), mem=None, align=0, **env):
    toks = lex(source)
    node = E.Parser(toks, 0, len(toks)).parse_expr()
    return domain._pred_match(node, list(types), mem, align, dict(env))


class PredicateModelTest(unittest.TestCase):
    def test_combinators_and_type_sets(self):
        s8, s16 = E.scalar(8), E.scalar(16)
        self.assertTrue(match("all(isScalar(0), typeIs(0, s8))",
                              [s8], s8=s8))
        self.assertFalse(match("any(isVector(0), typeIsNot(0, s8))",
                               [s8], s8=s8))
        self.assertTrue(match("typeInSet(0, {s8, s16})",
                              [s16], s8=s8, s16=s16))
        self.assertTrue(match("typePairInSet(0, 1, {{s8, s16}})",
                              [s8, s16], s8=s8, s16=s16))
        self.assertTrue(match("typeTupleInSet(0, 1, 2, {{s8, s16, s8}})",
                              [s8, s16, s8], s8=s8, s16=s16))

    def test_pointer_and_memory_queries(self):
        p0, p1 = E.ptr(0), E.ptr(1)
        self.assertTrue(match("isPointer(0, 1)", [p1]))
        self.assertFalse(match("isPointer(0, 1)", [p0]))
        s24 = E.scalar(24)
        self.assertTrue(match("memSizeInBytesNotPow2()", [], mem=s24))
        self.assertTrue(match(
            "typePairAndMemDescInSet(0, 1, 0, {{s8, s16, s24, 8}})",
            [E.scalar(8), E.scalar(16)], mem=s24, align=16,
            s8=E.scalar(8), s16=E.scalar(16), s24=s24))

    def test_opaque_predicates_stay_undecided(self):
        self.assertIsNone(match("immIs(0, 5)", [E.scalar(32)]))
        self.assertIsNone(match(
            "atomicOrderingAtLeastOrStrongerThan(0, Acquire)", []))


class ElementCountTest(unittest.TestCase):
    def test_fixed_and_scalable(self):
        ec = E.element_count("fixed", 4)
        self.assertTrue(evaluate("E.isVector()", E=ec))
        self.assertFalse(evaluate("E.isScalable()", E=ec))
        self.assertEqual(evaluate("E.getKnownMinValue()", E=ec), 4)
        self.assertEqual(evaluate("E.divideCoefficientBy(2)", E=ec),
                         E.element_count("fixed", 2))

    def test_scalable_count(self):
        ec = E.element_count("scalable", 4)
        self.assertTrue(evaluate("E.isScalable()", E=ec))
        self.assertFalse(evaluate("E.isFixed()", E=ec))
        self.assertEqual(evaluate("E.getKnownMinValue()", E=ec), 4)


if __name__ == "__main__":
    unittest.main()
