"""Manual harness regressions; run via just test-kde, never normal pytest."""

import unittest

from tools.highlight_engine_support import (
    NEWLINE_CASES,
    CorpusCase,
    EngineResult,
    assert_result,
    expected_spans,
)
from tools.highlight_kde_support import KDE_PROJECTION, parse_html

from agnette.agl.highlight.categories import Category


class EngineContractTests(unittest.TestCase):
    def test_style_collisions_are_projected_symmetrically(self) -> None:
        case = CorpusCase("collision", "and x")
        result = EngineResult(
            case.source,
            ((0, 3, Category.KEYWORD), (3, 5, Category.TEXT)),
        )
        assert_result(case, result, KDE_PROJECTION)
        self.assertEqual(expected_spans(case.source)[0][2], Category.OPERATOR_WORD)

    def test_wrong_core_style_fails(self) -> None:
        with self.assertRaises(AssertionError):
            assert_result(
                CorpusCase("number", "123"),
                EngineResult("123", ((0, 3, Category.STRING),)),
                KDE_PROJECTION,
            )

    def test_partition_and_roundtrip_failures_are_not_projected_away(self) -> None:
        case = CorpusCase("partition", "xy")
        for result in (
            EngineResult("x", ((0, 1, Category.TEXT),)),
            EngineResult("xy", ((1, 2, Category.TEXT),)),
            EngineResult("xy", ((0, 1, Category.TEXT),)),
            EngineResult("xy", ((0, 2, Category.TEXT), (1, 2, Category.TEXT))),
            EngineResult("xy", ((0, 0, Category.TEXT), (0, 2, Category.TEXT))),
        ):
            with self.subTest(result=result), self.assertRaises(AssertionError):
                assert_result(case, result, KDE_PROJECTION)

    def test_html_entities_nested_wrappers_and_unstyled_text(self) -> None:
        results = parse_html(
            '<pre><code class="sourceCode agl"><span id="cb1-1">'
            '<a href="#cb1-1"></a><span class="st">&quot;&lt;&amp;𐐀&quot;</span>'
            '\t\n<span class="dv">12</span></span></code></pre>'
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, '"<&𐐀"\t\n12')
        self.assertEqual(
            results[0].spans,
            ((0, 5, Category.STRING), (5, 7, Category.TEXT), (7, 9, Category.NUMBER)),
        )

    def test_unknown_style_fails_instead_of_becoming_plain_text(self) -> None:
        with self.assertRaises(ValueError):
            parse_html('<code><span class="unknown">x</span></code>')

    def test_shared_contract_preserves_original_newlines(self) -> None:
        for case in NEWLINE_CASES:
            with self.subTest(case=case.name):
                assert_result(case, EngineResult(case.source, expected_spans(case.source)))
                normalized = case.source.replace("\r\n", "\n").replace("\r", "\n")
                if normalized != case.source:
                    with self.assertRaises(AssertionError):
                        assert_result(case, EngineResult(normalized, expected_spans(normalized)))
                with self.assertRaises(AssertionError):
                    trimmed = case.source.strip()
                    assert_result(case, EngineResult(trimmed, expected_spans(trimmed)))

    def test_code_blocks_do_not_bleed_styles(self) -> None:
        results = parse_html('<code><span class="kw">let</span></code><code>x</code>')
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1], EngineResult("x", ((0, 1, Category.TEXT),)))


if __name__ == "__main__":
    unittest.main()
