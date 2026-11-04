import unittest

import testlib  # noqa: F401  (sets sys.path)
from x86_mir_corpus import lexer


class LexerTest(unittest.TestCase):
    def test_comments_do_not_swallow_code(self):
        src = ('// line\n'
               '/* block\n'
               '   still a comment */\n'
               'foo(/*Min=*/64, "a\\"b");\n')
        toks = lexer.lex(src)
        texts = [t.text for t in toks if t.kind != "eof"]
        self.assertEqual(texts[:2], ["foo", "("])
        self.assertNotIn("Min", texts)
        self.assertNotIn("block", texts)

        numbers = [t for t in toks if t.kind == "num"]
        self.assertEqual([(t.text, t.line) for t in numbers], [("64", 4)])
        strings = [t for t in toks if t.kind == "str"]
        self.assertEqual([(t.text, t.line) for t in strings], [('"a\\"b"', 4)])

    def test_preprocessor_line_is_one_token(self):
        toks = lexer.lex("#include <x>\nint y;\n")
        self.assertEqual(toks[0].kind, "pp")
        self.assertEqual(toks[0].text, "#include <x>")
        self.assertEqual((toks[1].text, toks[1].line), ("int", 2))

    def test_numbers(self):
        toks = lexer.lex("0x1F 0b101 64u 7")
        self.assertEqual([t.text for t in toks if t.kind == "num"],
                         ["0x1F", "0b101", "64u", "7"])

    def test_matching_nested_brackets(self):
        toks = lexer.lex("f({a,(b)}, [c]);")
        open_i = next(i for i, t in enumerate(toks) if t.text == "(")
        close_i = lexer.matching(toks, open_i)
        self.assertGreater(close_i, open_i)
        self.assertEqual(toks[close_i].text, ")")
        self.assertEqual(toks[close_i + 1].text, ";")

        brace_i = next(i for i, t in enumerate(toks) if t.text == "{")
        self.assertEqual(toks[lexer.matching(toks, brace_i)].text, "}")

    def test_unterminated_bracket_reports_missing(self):
        toks = lexer.lex("f(a")
        self.assertEqual(lexer.matching(toks, 1), -1)


if __name__ == "__main__":
    unittest.main()
