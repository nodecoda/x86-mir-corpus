import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import expr as E
from x86_mir_corpus import lexer


def parse_eval(src, env=None):
    node = E.Parser(lexer.lex(src)).parse_expr()
    return E.eval_expr(node, env or {})


class ExprEvalTest(unittest.TestCase):
    def test_nested_ternary_picks_the_config_vector(self):
        src = ("HasBWI ? LLT::fixed_vector(64, LLT::scalar(8)) : "
               "HasAVX2 ? LLT::fixed_vector(32, LLT::scalar(8)) : "
               "LLT::fixed_vector(16, LLT::scalar(8))")
        self.assertEqual(
            E.llt_name(parse_eval(src, {"HasBWI": 1, "HasAVX2": 1})), "v64s8")
        self.assertEqual(
            E.llt_name(parse_eval(src, {"HasBWI": 0, "HasAVX2": 1})), "v32s8")
        self.assertEqual(
            E.llt_name(parse_eval(src, {"HasBWI": 0, "HasAVX2": 0})), "v16s8")

    def test_precedence(self):
        self.assertEqual(parse_eval("1 + 2 * 3 == 7 ? 10 : 20"), 10)
        self.assertEqual(parse_eval("1 << 4 & 0xFF"), 16)
        self.assertEqual(parse_eval("!0 && 2 > 1"), True)

    def test_llt_names(self):
        self.assertEqual(E.llt_name(parse_eval("LLT::scalar(8)")), "s8")
        self.assertEqual(E.llt_name(parse_eval("LLT::pointer(0)")), "p0")
        self.assertEqual(E.llt_name(parse_eval("LLT::pointer(2)")), "p2")
        self.assertEqual(
            E.llt_name(parse_eval("LLT::fixed_vector(64, LLT::scalar(8))")),
            "v64s8")
        self.assertEqual(
            E.llt_name(parse_eval("LLT::fixed_vector(4, LLT::pointer(0))")),
            "v4p0")

    def test_subtarget_predicate_reads_the_feature_environment(self):
        env = {"__features__": {"hasAVX2": True, "hasBWI": False}}
        self.assertEqual(
            parse_eval("Subtarget.hasAVX2() ? 64 : 32", env), 64)
        self.assertEqual(
            parse_eval("Subtarget.hasBWI() ? 64 : 32", env), 32)

    def test_unknown_predicate_is_an_expression_error(self):
        with self.assertRaises(E.ExprError) as ctx:
            parse_eval("Subtarget.hasFancy()", {"__features__": {}})
        self.assertIn("hasFancy", str(ctx.exception))

    def test_unknown_identifier_is_an_expression_error(self):
        with self.assertRaises(E.EvalError):
            parse_eval("notDeclared")

    def test_predicate_object_call_keeps_its_arguments(self):
        node = E.Parser(lexer.lex("typeInSet(0, {v4s32})(Query)")).parse_expr()
        self.assertIsInstance(node, E.Call)
        self.assertEqual(node.name, "typeInSet")
        self.assertEqual(len(node.args), 3)
        self.assertIsInstance(node.args[1], E.ListLit)

    def test_lambda_keeps_params_and_body(self):
        src = ("[](unsigned TypeIdx, LLT Ty, const LegalityQuery &Q) "
               "{ return typeIs(0, Types[TypeIdx]); }")
        node = E.Parser(lexer.lex(src)).parse_expr()
        self.assertIsInstance(node, E.Lambda)
        self.assertIn("TypeIdx", node.params)
        self.assertTrue(node.body)
        self.assertTrue(E.lambda_has(node, "Types"))
        self.assertFalse(E.lambda_has_mem(node))

    def test_lambda_memdesc_is_detected(self):
        src = "[] (unsigned, LLT, const LegalityQuery &Q) { return Q.MMODescrs[0].SizeInBits == 8; }"
        node = E.Parser(lexer.lex(src)).parse_expr()
        self.assertTrue(E.lambda_has_mem(node))

    def test_parse_error_carries_a_line(self):
        with self.assertRaises(E.ExprError) as ctx:
            E.Parser(lexer.lex("LLT::scalar(\n8")).parse_expr()
        self.assertIn("line", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
