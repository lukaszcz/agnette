"""Generator structure and cheap target execution, without external engines."""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import (
    Capture,
    Grammar,
    Pop,
    Push,
    Rule,
    State,
    Switch,
    keyword_rule,
)
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.targets import TARGETS
from agnette.agl.highlight.targets.kde import KDE_STYLES
from agnette.agl.keywords import KEYWORDS, OPERATOR_SOFT_KEYWORDS, SOFT_KEYWORDS
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES
from agnette.util.ident import IDENT_STOP
from agnette.version import AGM_VERSION

type _SExpression = str | list[_SExpression]


def _elisp_forms(source: str) -> list[_SExpression]:
    """Read inventory lists, strings, symbols and quotation; reject unbalanced forms."""
    tokens = re.findall(r';[^\n]*|"(?:\\.|[^"\\])*"|[()\']|[^\s()\'";]+', source)
    tokens = [token for token in tokens if not token.startswith(";")]
    position = 0

    def expression() -> _SExpression:
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "(":
            result: list[_SExpression] = []
            while tokens[position] != ")":
                result.append(expression())
            position += 1
            return result
        if token == "'":
            return ["quote", expression()]
        assert token != ")"
        return json.loads(token) if token.startswith('"') else token

    forms = []
    while position < len(tokens):
        forms.append(expression())
    return forms


def test_emacs_inventory_is_deterministic_balanced_and_canonical() -> None:
    output = TARGETS["emacs"](AGL_GRAMMAR)
    assert output == TARGETS["emacs"](AGL_GRAMMAR)
    # The inventory is a vocabulary target, independent of grammar state topology.
    assert output == TARGETS["emacs"](Grammar((State("empty", ()),), "empty"))
    forms = _elisp_forms(output)
    definitions = {form[1]: form[2] for form in forms if form[0] == "defconst"}
    inventories = {
        "agl-keywords": KEYWORDS,
        "agl-constant-keywords": {"true", "false", "null"},
        "agl-operator-keywords": OPERATOR_SOFT_KEYWORDS,
        "agl-soft-keywords": SOFT_KEYWORDS,
        "agl-contextual-builtins": set(BUILTIN_CALL_NAMES),
        "agl-primitive-type-names": set(BUILTIN_TYPE_NAMES),
    }
    assert set(definitions) == {*inventories, "agl--ident-stop-chars"}
    for name, expected in inventories.items():
        assert definitions[name] == ["quote", sorted(expected)]
    stops = definitions["agl--ident-stop-chars"]
    assert isinstance(stops, str)
    assert set(stops) == IDENT_STOP
    assert len(stops) == len(IDENT_STOP)
    assert stops.startswith("][")  # Safe as a bracket-expression body in Emacs.
    assert forms[-1] == ["provide", ["quote", "agl-inventory"]]


def _xml(grammar: Grammar = AGL_GRAMMAR) -> ET.Element:
    return ET.fromstring(TARGETS["kde"](grammar))


def _contexts(root: ET.Element) -> dict[str, ET.Element]:
    return {node.attrib["name"]: node for node in root.findall("./highlighting/contexts/context")}


def _destinations(action: str) -> list[str]:
    return [name for name in action.replace("#pop", "").split("!") if name and name != "#stay"]


def test_kde_registration_determinism_metadata_and_structure() -> None:
    output = TARGETS["kde"](AGL_GRAMMAR)
    assert output == TARGETS["kde"](AGL_GRAMMAR)
    root = ET.fromstring(output)
    assert root.attrib["name"] == "AgL"
    assert root.attrib["extensions"] == "*.agl"
    major, minor, patch = map(int, AGM_VERSION.split("."))
    assert root.attrib["version"] == str(major * 1_000_000 + minor * 1000 + patch)
    assert root.attrib["section"] == "Scripts"
    assert f"AgL release {AGM_VERSION}" in output
    contexts = _contexts(root)
    assert len(contexts) == len(root.findall("./highlighting/contexts/context"))
    assert len(contexts) < 30  # Shared finite stages, not a stack/state cross product.
    items = {
        node.attrib["name"]: node for node in root.findall("./highlighting/itemDatas/itemData")
    }
    assert set(items) == {category.value for category in Category}
    assert set(KDE_STYLES) == set(Category)
    for category, style in KDE_STYLES.items():
        assert items[category.value].attrib["defStyleNum"] == style
    lists = {node.attrib["name"] for node in root.findall("./highlighting/list")}
    for context in contexts.values():
        assert context.attrib["attribute"] in items
        for key in ("lineEndContext", "lineEmptyContext", "fallthroughContext"):
            for name in _destinations(context.get(key, "#stay")):
                assert name in contexts
        for rule in context:
            assert rule.attrib["attribute"] in items
            for name in _destinations(rule.attrib["context"]):
                assert name in contexts
            if rule.tag == "keyword":
                assert rule.attrib["String"] in lists
            else:
                assert rule.tag == "RegExpr"
                assert rule.attrib["String"]
                re.compile(rule.attrib["String"])
    assert root.find(".//keyword") is not None
    assert "weakDeliminator" not in output


def test_kde_keyword_lists_are_canonical() -> None:
    lists = {
        frozenset(item.text for item in node) for node in _xml().findall("./highlighting/list")
    }
    constants = {"true", "false", "null"}
    assert lists == {
        frozenset(constants),
        KEYWORDS - constants,
        SOFT_KEYWORDS - OPERATOR_SOFT_KEYWORDS,
        OPERATOR_SOFT_KEYWORDS,
        frozenset(BUILTIN_TYPE_NAMES),
        frozenset(BUILTIN_CALL_NAMES),
    }


def test_kde_regex_xml_escaping_and_initial_context() -> None:
    pattern = r'^<&"\[\]\\\+\?\{\}$'
    grammar = Grammar(
        (State('other<&"', ()), State('main<&"', (Rule(pattern, Category.NAME),))), 'main<&"'
    )
    contexts = list(_contexts(_xml(grammar)).values())
    assert contexts[0].find("RegExpr").attrib["String"] == pattern
    assert contexts[0].attrib["attribute"] == Category.TEXT.value


@pytest.mark.parametrize(
    "character",
    [chr(point) for point in range(32) if point not in (9, 10, 13)]
    + ["\ud800", "\udbff", "\udc00", "\udfff", "\ufffe", "\uffff"],
)
@pytest.mark.parametrize("field", ["pattern", "capture", "words", "state"])
def test_kde_rejects_xml_forbidden_characters(character: str, field: str) -> None:
    name = f"root{character}" if field == "state" else "root"
    rule = Rule("x", Category.KEYWORD)
    if field == "pattern":
        rule = Rule(character, Category.KEYWORD)
    elif field == "capture":
        rule = Rule(f"({character})", captures=(Capture(1, Category.KEYWORD),))
    elif field == "words":
        rule = Rule("x", Category.KEYWORD, words=(f"x{character}",))
    # These remain valid target-neutral model inputs.
    grammar = Grammar((State(name, (rule,)),), name)
    with pytest.raises(ValueError):
        TARGETS["kde"](grammar)


@pytest.mark.parametrize(
    "character",
    ["\t", "\n", "\r", "\x20", "\ud7ff", "\ue000", "\ufffd", "\U00010000", "\U0010ffff"],
)
def test_kde_preserves_xml_legal_pattern_boundaries(character: str) -> None:
    grammar = Grammar(
        (State(f"root{character}", (Rule(character, Category.KEYWORD),)),), f"root{character}"
    )
    rule = _xml(grammar).find("./highlighting/contexts/context/RegExpr")
    assert rule is not None
    assert rule.attrib["String"] == character


@pytest.mark.parametrize(
    "character",
    ["\t", "\n", "\r", "\r\n", "\x20", "\ud7ff", "\ue000", "\ufffd", "\U00010000", "\U0010ffff"],
)
def test_kde_preserves_xml_legal_keyword_text(character: str) -> None:
    word = f"x{character}y"
    grammar = Grammar((State("root", (Rule("x", Category.KEYWORD, words=(word,)),)),))
    item = _xml(grammar).find("./highlighting/list/item")
    assert item is not None
    assert item.text == word


def test_kde_preserves_escaped_form_feed_regex() -> None:
    grammar = Grammar((State("root", (Rule(r"\f", Category.KEYWORD),)),))
    rule = _xml(grammar).find("./highlighting/contexts/context/RegExpr")
    assert rule is not None
    assert rule.attrib["String"] == r"\f"
    assert re.fullmatch(rule.attrib["String"], "\f") is not None


def _render(grammar: Grammar, source: str) -> list[Category]:
    """Interpret emitted XML's native transition vocabulary, one line at a time."""
    root = _xml(grammar)
    contexts = _contexts(root)
    lists = {
        node.attrib["name"]: {item.text for item in node}
        for node in root.findall("./highlighting/list")
    }
    stack = [next(iter(contexts))]
    colors: list[Category] = []

    def transition(action: str) -> None:
        while action.startswith("#pop"):
            if len(stack) > 1:
                stack.pop()
            action = action[4:]
        name = action.lstrip("!")
        if name and name != "#stay":
            stack.append(name)

    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        position = 0
        visited: set[tuple[str, int, int]] = set()
        while position < len(body):
            context = contexts[stack[-1]]
            for index, rule in enumerate(context):
                key = (stack[-1], index, len(stack))
                if key in visited or (rule.get("column") == "0" and position != 0):
                    continue
                if rule.tag == "keyword":
                    match = re.match(r"[A-Za-z0-9_]+", body[position:])
                    end = (
                        position + match.end()
                        if match and match.group() in lists[rule.attrib["String"]]
                        else position
                    )
                else:
                    match = re.compile(rule.attrib["String"]).match(body, position)
                    if match is None:
                        continue
                    end = match.end()
                if end == position and rule.get("lookAhead") != "true":
                    continue
                if rule.get("lookAhead") == "true":
                    end = position
                if end == position:
                    visited.add(key)
                else:
                    visited.clear()
                colors.extend([Category(rule.attrib["attribute"])] * (end - position))
                position = end
                transition(rule.attrib["context"])
                break
            else:
                colors.append(Category(context.attrib["attribute"]))
                position += 1
                visited.clear()
        while True:
            context = contexts[stack[-1]]
            action = context.get("lineEmptyContext" if not body else "lineEndContext", "#stay")
            previous = list(stack)
            transition(action)
            if stack == previous or not action.startswith("#pop"):
                break
        colors.extend([Category.TEXT] * (len(line) - len(body)))
    return colors


def _expected(grammar: Grammar, source: str) -> list[Category]:
    return [
        category for start, end, category in evaluate(grammar, source) for _ in range(end - start)
    ]


@pytest.mark.parametrize(
    "source",
    [
        "@ field record Thing | Some\nexception Problem\ntype Alias enum Choice",
        "@\trecord @true record true | enum @\nrecord\nThing",
        "resource-path resource-dir( resource( as? a+b xrecord record-id",
        '"a%{ record Thing @ field | Some }b"',
        '"unfinished\nlet x = 2',
        '"""%{ {{{x\nlet y=2',
        '"""bad\\q consumed"""\ntail""" let x=1',
        "$\n  hi\n\n  tail\nlet x=1",
        "$ text %{ @ field } tail\nlet z=3",
        "$\n  body\nnext",
        '"bad\\q\nlet x=3',
    ],
)
def test_kde_builtin_capture_context_and_state_contract(source: str) -> None:
    assert _render(AGL_GRAMMAR, source) == _expected(AGL_GRAMMAR, source)


@pytest.mark.parametrize("action", [None, Push("next"), Pop(), Switch("next")])
@pytest.mark.parametrize("words", [(), ("aaa",)])
def test_kde_generic_staged_captures_apply_actions_once(
    action: Push | Pop | Switch | None, words: tuple[str, ...]
) -> None:
    captured = Rule(
        r"(a+)([ -]*)(b+)",
        Category.PUNCTUATION,
        (Capture(3, Category.NUMBER), Capture(1, Category.NAME)),
        action,
        words,
    )
    grammar = Grammar(
        (
            State("root", (Rule("<", action=Push("inner")),), Category.ERROR),
            State("inner", (Rule(">", action=Pop()), captured), Category.STRING),
            State("next", (Rule(">", action=Pop()),), Category.OPERATOR),
        )
    )
    source = "<aaa -bbb!>x"
    assert _render(grammar, source) == _expected(grammar, source)


def test_kde_generic_capture_backtracking_optional_empty_and_background() -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    Rule(
                        r"^(a+)(a)(b)?()$",
                        Category.OPERATOR,
                        (
                            Capture(1, Category.NAME),
                            Capture(3, Category.NUMBER),
                            Capture(4, Category.ERROR),
                        ),
                    ),
                ),
            ),
        )
    )
    for source in ("aaab", "aaa", "aaab\nz", "aaa\nz"):
        assert _render(grammar, source) == _expected(grammar, source)


def test_kde_capture_action_after_empty_tail_at_line_end() -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    Rule(
                        r"(a)(b)?()$",
                        captures=(Capture(1, Category.KEYWORD),),
                        action=Switch("body"),
                    ),
                ),
                Category.ERROR,
            ),
            State("body", (), Category.NAME),
        )
    )
    for source in ("a\nz", "ab\nz"):
        assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize(
    "pattern", [r"(?:(a))", r"(a)+", r"(a)|(b)", r"x(?:(a)|b)y", r"(a)?", r"(?<=x)(a)?"]
)
def test_kde_rejects_captures_outside_linear_partition_subset(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern, captures=(Capture(1, Category.NAME),)),)),))
    with pytest.raises(ValueError):
        TARGETS["kde"](grammar)


def test_kde_line_end_and_empty_line_native_actions() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("^$", action=Switch("body")), Rule("x", action=Push("inner")))),
            State("inner", (Rule("$", action=Pop()),), Category.STRING),
            State("body", (), Category.NAME),
        )
    )
    for source in ("xy\nz", "\nx"):
        assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize("pattern", [r"(?<=x)$", r"(?<=x)", r"x*$(?<!y)", r"(?=x$)", r"^a*"])
def test_kde_rejects_context_dependent_line_end_actions(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern, action=Switch("next")),)), State("next", ())))
    with pytest.raises(ValueError):
        TARGETS["kde"](grammar)


@pytest.mark.parametrize(
    "pattern, source",
    [
        (r"^(?:a|b)$", "b\nzb\na"),
        (r"(?:ab){0,1}x$", "abx\nz"),
        (r"(?:ab){1}?x$", "abx\nz"),
        (r"(?:x|y)+$", "xy\nz"),
        (r"[()$]+", "($)z"),
        (r"[]()]+", "]()z"),
        (r"\[\(\?\)\]", "[(?)]z"),
    ],
)
def test_kde_consuming_generic_actions_and_structural_regexes(pattern: str, source: str) -> None:
    grammar = Grammar(
        (
            State("root", (Rule(pattern, Category.KEYWORD, action=Switch("body")),)),
            State("body", (), Category.NUMBER),
        )
    )
    assert _render(grammar, source) == _expected(grammar, source)


def test_kde_unconditional_zero_width_switch_runs_on_empty_lines() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("", Category.ERROR), Rule("", action=Switch("body")))),
            State(
                "body",
                (keyword_rule(("let", "if"), Category.KEYWORD, token_start=True),),
                Category.NUMBER,
            ),
        )
    )
    root = _xml(grammar)
    assert all(node.attrib["String"] for node in root.findall(".//RegExpr"))
    initial = next(iter(_contexts(root).values()))
    assert initial.attrib["lineEndContext"] == "#pop!state1"
    assert initial.attrib["lineEmptyContext"] == "#pop!state1"
    assert len(initial) == 1
    assert initial[0].attrib["lookAhead"] == "true"
    assert re.fullmatch(initial[0].attrib["String"], "x") is not None
    for source in ("let x if", "\nlet x", "\n\nif\nlet", "!\nif"):
        assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize("action", [Push("body"), Switch("body"), Pop()])
def test_kde_empty_transition_rules_have_only_nonempty_regexes(action: Push | Switch | Pop) -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    Rule("<", action=Push("inner")),
                    keyword_rule(("let",), Category.KEYWORD, token_start=True),
                ),
            ),
            State("inner", (Rule(">", action=Pop()), Rule("", action=action))),
            State(
                "body",
                (
                    Rule(">", action=Pop()),
                    keyword_rule(("let",), Category.KEYWORD, token_start=True),
                ),
                Category.NUMBER,
            ),
        )
    )
    assert all(node.attrib["String"] for node in _xml(grammar).findall(".//RegExpr"))
    for source in ("<let x>", "<\nlet x>", "<\n\nlet x>"):
        assert _render(grammar, source) == _expected(grammar, source)


def test_kde_shared_switch_destinations_preserve_base_frame_paths() -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (Rule("a", action=Switch("body")), Rule("b", action=Switch("next"))),
            ),
            State("body", (Rule("d", action=Switch("next")),), Category.NAME),
            State("next", (Rule("c", action=Switch("body")),), Category.NUMBER),
        )
    )
    for source in ("ax", "bcx", "bxx", "adcdcx"):
        assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize(
    "pattern, words, source",
    [
        ("x", ("xx",), "xx tail"),
        ("x", ("x?",), "x? tail"),
        ("x", ("not an identifier",), "x tail"),
        ("x", ("x\ty", "x\ny", "x\r\ny", "x\ry"), "x tail"),
        ("x", ("",), "x tail"),
        ("x(?=x)", ("xx",), "xx tail"),
        ("x+", ("x",), "xxx tail"),
    ],
)
def test_kde_keyword_metadata_does_not_change_match_extent(
    pattern: str, words: tuple[str, ...], source: str
) -> None:
    grammar = Grammar(
        (
            State("root", (Rule(pattern, Category.KEYWORD, action=Switch("body"), words=words),)),
            State("body", (), Category.NAME),
        )
    )
    root = _xml(grammar)
    assert root.find(".//keyword") is None
    assert tuple(item.text or "" for item in root.findall("./highlighting/list/item")) == words
    assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize("suffix", ["", r"(?=[ \t]*\()"])
def test_kde_keyword_optimization_requires_exact_helper_pattern(suffix: str) -> None:
    rule = keyword_rule(("resource",), Category.BUILTIN_CALL, token_start=True)
    rule = replace(rule, pattern=rule.pattern + suffix, action=Switch("body"))
    grammar = Grammar((State("root", (rule,)), State("body", (), Category.NUMBER)))
    assert (_xml(grammar).find(".//keyword") is not None) == (not suffix)
    for source in ("resource( x", "resource x", "resource-path"):
        assert _render(grammar, source) == _expected(grammar, source)


def test_kde_keyword_metadata_preserves_metacharacters_and_actions() -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    keyword_rule(
                        ("foo", "foo?", "a+b", 'foo"bar', "αβ"),
                        Category.KEYWORD,
                        action=Switch("body"),
                        token_start=True,
                    ),
                ),
            ),
            State("body", (), Category.NAME),
        )
    )
    assert _xml(grammar).find(".//keyword") is not None
    for source in ("foo? tail", "foo tail", "a+b tail", 'foo"bar tail', "αβ tail"):
        assert _render(grammar, source) == _expected(grammar, source)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_kde_line_local_anchors_fallback_and_empty_noop(newline: str) -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (Rule(""), Rule("^a$", Category.KEYWORD), Rule("^b$", Category.NAME)),
                Category.ERROR,
            ),
        )
    )
    source = "a" + newline + "b" + newline + "!"
    assert _render(grammar, source) == _expected(grammar, source)
    assert all(node.attrib["String"] for node in _xml(grammar).findall(".//RegExpr"))
