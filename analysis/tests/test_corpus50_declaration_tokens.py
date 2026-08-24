from __future__ import annotations

import re
import time

import pytest

from analysis import corpus50


VARIABLE = "\u53d8\u91cf"
TYPE_NAME = "\u7c7b\u578b"
NAME = "\u540d\u79f0"
DATA = "\u0414\u0430\u043d\u043d\u044b\u0435"


def legacy_declaration_tokens(line: str) -> set[str]:
    """Frozen oracle for the pre-optimization declaration matcher."""

    tokens = {match.group(0) for match in corpus50.IDENTIFIER_RE.finditer(line)}
    declarations: set[str] = set()
    for token in tokens:
        escaped = re.escape(token)
        keyword_pattern = (
            rf"\b(?:{'|'.join(corpus50.DECLARATION_KEYWORDS)})\s+{escaped}\b"
        )
        assignment_pattern = (
            rf"\b{escaped}\b\s*(?:(?::\s*[^=,:]+)?\s*)(?::=|=(?!=))"
        )
        if re.search(keyword_pattern, line) or re.search(assignment_pattern, line):
            declarations.add(token)
    return declarations


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"def {VARIABLE}(value)", {VARIABLE}),
        (f"def def {VARIABLE}", {"def", VARIABLE}),
        (f"x.def {NAME}", {NAME}),
        (f"0def {NAME}", set()),
        (f"\u51fd\u6570 {NAME}", set()),
        (f"{VARIABLE} = 1", {VARIABLE}),
        (f"{VARIABLE} := 1", {VARIABLE}),
        # Every token is evaluated independently by the old matcher: both the
        # annotated target and the type token immediately before ``=`` count.
        (f"x: {TYPE_NAME} = value", {"x", TYPE_NAME}),
        ("x: dict[str, int] = value", set()),
        (f"x: {TYPE_NAME} == value", set()),
        ("x:   = value", {"x"}),
        (f"x: {TYPE_NAME} := value", {"x", TYPE_NAME}),
        (f"1{VARIABLE} = value", set()),
        (f"_{VARIABLE} = value", {f"_{VARIABLE}"}),
        (f"emoji\U0001f642{VARIABLE} = value", {VARIABLE}),
        ("x = = value", {"x"}),
        ("x === value", set()),
        ("const ma\u00f1ana = 1", {"ma\u00f1ana"}),
        ("let \u03bb\t= 2", {"\u03bb"}),
        (f"type {DATA} struct {{}}", {DATA}),
    ],
)
def test_declaration_tokens_match_legacy_semantics(
    line: str, expected: set[str]
) -> None:
    assert legacy_declaration_tokens(line) == expected
    assert corpus50._declaration_tokens(line) == expected


def test_every_frozen_english_declaration_keyword_retains_legacy_semantics() -> None:
    for keyword in corpus50.DECLARATION_KEYWORDS:
        line = f"{keyword}\t{VARIABLE}"
        assert legacy_declaration_tokens(line) == {VARIABLE}
        assert corpus50._declaration_tokens(line) == {VARIABLE}


def test_sanitized_comments_and_strings_do_not_create_declarations() -> None:
    source = "\n".join(
        [
            f"{VARIABLE} = 1 # \u6ce8\u91ca\u53d8\u91cf = 2",
            'other = "\u5b57\u7b26\u4e32\u53d8\u91cf = 3"',
            "// \u884c\u6ce8\u91ca = 4",
            "/* \u5757\u53d8\u91cf = 5",
            "\u4ecd\u5728\u5757\u4e2d = 6 */",
            f"print({VARIABLE})",
        ]
    )
    declarations: set[str] = set()
    for line in corpus50._sanitize_source(source):
        assert corpus50._declaration_tokens(line) == legacy_declaration_tokens(line)
        declarations.update(corpus50._declaration_tokens(line))
    assert declarations == {VARIABLE, "other"}


def test_multi_megabyte_unique_token_line_completes_quickly() -> None:
    line = " ".join(f"ordinary_identifier_{index}" for index in range(100_000))
    assert len(line.encode("utf-8")) >= 2 * 1024 * 1024

    started = time.perf_counter()
    declarations = corpus50._declaration_tokens(line)
    elapsed = time.perf_counter() - started

    assert declarations == set()
    assert elapsed < 5.0, f"declaration scan took {elapsed:.3f}s"
