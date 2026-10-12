"""KSyntaxHighlighting XML with native stack actions and finite capture stages.

Styled captures must be top-level, unrepeated groups in a concatenation (an
optional group is supported). Background gaps and nested unstyled groups retain
ordinary portable regex syntax. Suffix lookaheads preserve ordered backtracking
choices; shared temporary contexts style each partition and apply the action only
at its end. No capture substitution or stack cross product is needed.

KDE does not run RegExpr at EOL. Unconditional end actions and empty-line actions
use native context attributes; context-dependent zero-width end actions are not
exportable. Nullable native transitions use an explicit lookahead fallback after
any consuming rule; nontransitioning empty rules are omitted.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import (
    Action,
    Grammar,
    Pop,
    Push,
    Rule,
    State,
    Switch,
    keyword_rule,
)
from agnette.agl.highlight.targets._kde_contract import validate
from agnette.agl.highlight.targets._regex import Structure, analyze
from agnette.util.ident import IDENT_STOP, is_identifier
from agnette.version import AGM_VERSION

KDE_STYLES: dict[Category, str] = {
    Category.TEXT: "dsNormal",
    Category.COMMENT: "dsComment",
    Category.KEYWORD: "dsKeyword",
    Category.KEYWORD_CONSTANT: "dsConstant",
    Category.OPERATOR_WORD: "dsKeyword",
    Category.TYPE: "dsDataType",
    Category.CONSTRUCTOR: "dsFunction",
    Category.FUNCTION: "dsFunction",
    Category.BUILTIN_CALL: "dsBuiltIn",
    Category.MODULE: "dsImport",
    Category.ATTRIBUTE: "dsAttribute",
    Category.PARAMETER: "dsVariable",
    Category.LOCAL: "dsVariable",
    Category.NAME: "dsNormal",
    Category.STRING: "dsString",
    Category.STRING_ESCAPE: "dsSpecialChar",
    Category.INTERPOLATION: "dsSpecialChar",
    Category.VERBATIM: "dsVerbatimString",
    Category.ENV_HOLE: "dsSpecialChar",
    Category.NUMBER: "dsDecVal",
    Category.OPERATOR: "dsOperator",
    Category.PUNCTUATION: "dsNormal",
    Category.PLACEHOLDER: "dsNormal",
    Category.ERROR: "dsError",
}


def _action(action: Action | None, names: dict[str, str], *, staged: bool = False) -> str:
    prefix = "#pop" if staged else ""
    if isinstance(action, Push):
        return prefix + ("!" if prefix else "") + names[action.state]
    if isinstance(action, Switch):
        return prefix + "#pop!" + names[action.state]
    if isinstance(action, Pop):
        return prefix + "#pop"
    return prefix or "#stay"


def _regex(
    parent: ET.Element,
    pattern: str,
    category: Category,
    action: str,
    *,
    lookahead: bool = False,
    nullable: bool = False,
    column_zero: bool = False,
) -> None:
    attributes = {"String": pattern, "attribute": category.value, "context": action}
    if column_zero:
        attributes["column"] = "0"
    if lookahead:
        attributes["lookAhead"] = "true"
    if pattern:
        ET.SubElement(parent, "RegExpr", attributes)
    if nullable:
        # Native RegExpr rejects empty strings and empty matches, even with
        # lookAhead. Probe one character after any consuming attempt; EOL
        # instead uses the context attributes.
        ET.SubElement(
            parent, "RegExpr", attributes | {"String": f"(?:{pattern}).", "lookAhead": "true"}
        )


def _ends(rule: Rule, structure: Structure) -> tuple[bool, bool]:
    """Recognize native EOL actions; reject nullable context-dependent variants."""
    if rule.action is None or not structure.nullable:
        return False, False
    if rule.pattern == "^$":
        return False, True
    # A leading end alternative wins on every line, including empty lines.
    if rule.pattern == "$" or rule.pattern.startswith("$|"):
        return True, True
    if structure.end_anchor:
        raise ValueError("KDE cannot express conditional line-end actions")
    if rule._compiled.match("") is not None:
        if structure.start_anchor:
            raise ValueError("KDE cannot express start-dependent line-end actions")
        return True, True
    return False, False


def _keyword(rule: Rule) -> bool:
    """Metadata alone cannot determine a rule's matching extent."""
    return (
        bool(rule.words)
        and not rule.captures
        and all(is_identifier(word) for word in rule.words)
        and rule.pattern == keyword_rule(rule.words, rule.category, token_start=True).pattern
    )


def generate(grammar: Grammar) -> str:
    """Export a grammar deterministically, without files or optional dependencies."""
    structures = {
        rule: analyze(rule.pattern, rule.category, rule.captures)
        for state in grammar.states
        for rule in state.rules
    }
    validate(grammar, structures)
    major, minor, patch = map(int, AGM_VERSION.split("."))
    root = ET.Element(
        "language",
        name="AgL",
        section="Scripts",
        extensions="*.agl",
        version=str(major * 1_000_000 + minor * 1000 + patch),
        kateversion="5.0",
    )
    root.append(ET.Comment(f"AgL release {AGM_VERSION}"))
    highlighting = ET.SubElement(root, "highlighting")
    inventories = sorted(
        {rule.words for state in grammar.states for rule in state.rules if rule.words}
    )
    lists = {words: f"words{index}" for index, words in enumerate(inventories)}
    for words, name in lists.items():
        inventory = ET.SubElement(highlighting, "list", name=name)
        for word in sorted(words):
            ET.SubElement(inventory, "item").text = word
    contexts = ET.SubElement(highlighting, "contexts")

    def initial_first(state: State) -> bool:
        return state.name != grammar.initial

    ordered = sorted(grammar.states, key=initial_first)
    # Internal names cannot collide with arbitrary user state names or KDE syntax.
    names = {state.name: f"state{index}" for index, state in enumerate(ordered)}
    helpers: dict[Rule, str] = {}

    def context(name: str, category: Category) -> ET.Element:
        return ET.SubElement(
            contexts,
            "context",
            name=name,
            attribute=category.value,
            lineEndContext="#stay",
            lineEmptyContext="#stay",
        )

    def lower(parent: ET.Element, rule: Rule) -> None:
        action = _action(rule.action, names)
        keyword = _keyword(rule)
        if not rule.captures and not keyword:
            if rule.pattern or rule.action is not None:
                _regex(
                    parent,
                    rule.pattern,
                    rule.category,
                    action,
                    nullable=structures[rule].nullable if rule.action is not None else False,
                    column_zero=structures[rule].start_anchor,
                )
            return
        if rule in helpers:
            entry = helpers[rule]
        else:
            entry = f"helper{len(helpers)}"
            helpers[rule] = entry
            if keyword:
                stage = context(entry, rule.category)
                # Native keyword delimiters may split identifiers containing
                # punctuation. Consume those alternatives first; the full guard
                # retains the canonical helper's following boundary.
                unsafe = [
                    word for word in rule.words if re.fullmatch("[A-Za-z0-9_]+", word) is None
                ]
                if unsafe:
                    alternatives = "|".join(re.escape(word) for word in unsafe)
                    _regex(
                        stage,
                        f"(?={rule.pattern})(?:{alternatives})",
                        rule.category,
                        _action(rule.action, names, staged=True),
                    )
                ET.SubElement(
                    stage,
                    "keyword",
                    String=lists[rule.words],
                    attribute=rule.category.value,
                    context=_action(rule.action, names, staged=True),
                )
                # Explicit Unicode identifiers need not follow the engine's word
                # scanner, so preserve the same regex as a keyword fallback.
                _regex(stage, rule.pattern, rule.category, _action(rule.action, names, staged=True))
            else:
                facts = structures[rule]
                if facts.nullable:
                    raise ValueError("KDE captured rules must consume text")
                parts = facts.parts
                for index, (pattern, category) in enumerate(parts):
                    stage = context(f"{entry}-{index}", rule.category)
                    # Entering a stage commits a successful full match. At EOL
                    # its remaining partitions necessarily have zero width;
                    # bypass them because native RegExpr does not run there.
                    finish = _action(rule.action, names, staged=True)
                    stage.set("lineEndContext", finish)
                    stage.set("lineEmptyContext", finish)
                    suffix = "".join(part for part, _ in parts[index + 1 :])
                    transition = (
                        f"#pop!{entry}-{index + 1}"
                        if suffix
                        else _action(rule.action, names, staged=True)
                    )
                    _regex(
                        stage,
                        f"(?:{pattern})(?={suffix})",
                        category,
                        transition,
                        nullable=analyze(pattern).nullable,
                        column_zero=pattern.startswith("^"),
                    )
                entry += "-0"
                helpers[rule] = entry
        _regex(
            parent,
            rule.pattern,
            rule.category,
            entry,
            lookahead=True,
            column_zero=structures[rule].start_anchor,
        )

    # Add real states first, so the first context is always the initial state.
    nodes = {state.name: context(names[state.name], state.default) for state in ordered}
    for state in ordered:
        node = nodes[state.name]
        for rule in state.rules:
            end, empty = (
                _ends(rule, structures[rule]) if rule.action is not None else (False, False)
            )
            if end and node.attrib["lineEndContext"] == "#stay":
                node.set("lineEndContext", _action(rule.action, names))
            if empty and node.attrib["lineEmptyContext"] == "#stay":
                node.set("lineEmptyContext", _action(rule.action, names))
            lower(node, rule)
    items = ET.SubElement(highlighting, "itemDatas")
    for category, style in KDE_STYLES.items():
        ET.SubElement(items, "itemData", name=category.value, defStyleNum=style)
    general = ET.SubElement(root, "general")
    ET.SubElement(
        general, "keywords", casesensitive="1", additionalDeliminator="".join(sorted(IDENT_STOP))
    )
    ET.indent(root, space="  ")
    # XML normalizes literal CR in text; attributes already use references.
    xml = ET.tostring(root, encoding="unicode").replace("\r", "&#13;")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"
