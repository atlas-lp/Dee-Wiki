"""
tests/test_auto_wiki_builder.py — Tests for scripts/auto_wiki_builder.py helpers.
"""

import pytest

from auto_wiki_builder import (
    _sanitize_slug,
    _strip_code_fences,
    _build_index,
)


# ---------------------------------------------------------------------------
# _sanitize_slug
# ---------------------------------------------------------------------------

def test_sanitize_slug_basic():
    assert _sanitize_slug("Discounted Cash Flow") == "discounted-cash-flow"


def test_sanitize_slug_special_chars():
    # Use ASCII-only input: _sanitize_slug keeps \w chars (includes unicode β),
    # so avoid non-ASCII to get a result strictly in [a-z0-9-].
    result = _sanitize_slug("Beta & Risk! (v2)")
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in result)


def test_sanitize_slug_max_80():
    long_input = "word " * 50  # well over 80 chars
    result = _sanitize_slug(long_input)
    assert len(result) <= 80


def test_sanitize_slug_consecutive_hyphens_collapsed():
    result = _sanitize_slug("hello -- world")
    assert "--" not in result
    assert "hello" in result
    assert "world" in result


def test_sanitize_slug_strips_leading_trailing():
    result = _sanitize_slug("--hello--")
    assert not result.startswith("-")
    assert not result.endswith("-")


def test_sanitize_slug_empty():
    assert _sanitize_slug("") == ""


def test_sanitize_slug_already_clean():
    assert _sanitize_slug("clean-slug") == "clean-slug"


# ---------------------------------------------------------------------------
# _strip_code_fences
# ---------------------------------------------------------------------------

def test_strip_code_fences_markdown():
    text = "```markdown\n# Heading\n```"
    result = _strip_code_fences(text)
    assert result == "# Heading"


def test_strip_code_fences_json():
    text = "```json\n{}\n```"
    result = _strip_code_fences(text)
    assert result == "{}"


def test_strip_code_fences_no_fences_unchanged():
    text = "plain text here"
    result = _strip_code_fences(text)
    assert result == text


def test_strip_code_fences_yaml():
    text = "```yaml\ntype: concept\n```"
    result = _strip_code_fences(text)
    assert "yaml" not in result
    assert "type: concept" in result


# ---------------------------------------------------------------------------
# _build_index
# ---------------------------------------------------------------------------

def _make_inventory(**overrides):
    base = {
        "persona":     [],
        "concepts":    [],
        "entities":    [],
        "stubs":       [],
        "synthesized": [],
        "other":       [],
    }
    base.update(overrides)
    return base


def _make_entry(title, path, slug, desc=""):
    return {"title": title, "path": f"wiki/{path}", "slug": slug, "description": desc}


def test_build_index_contains_sections():
    inv = _make_inventory(
        concepts=[_make_entry("CAPM", "concepts/capm.md", "capm")],
        entities=[_make_entry("Sharpe", "entities/sharpe.md", "sharpe")],
        stubs=[_make_entry("Damodaran", "stubs/damodaran.md", "damodaran")],
        synthesized=[_make_entry("Syn Page", "synthesized/syn-page.md", "syn-page")],
    )
    result = _build_index(inv)
    assert "## Extracted Concepts" in result
    assert "## Extracted Entities" in result
    assert "## RAG Stubs"          in result
    assert "## Query-Synthesized"  in result


def test_build_index_total_count():
    inv = _make_inventory(
        concepts=[
            _make_entry("CAPM",  "concepts/capm.md",  "capm"),
            _make_entry("WACC",  "concepts/wacc.md",  "wacc"),
        ],
        entities=[_make_entry("Sharpe", "entities/sharpe.md", "sharpe")],
    )
    result = _build_index(inv)
    assert "3 pages" in result


def test_build_index_entry_link_format():
    inv = _make_inventory(
        concepts=[_make_entry("CAPM", "wiki/concepts/capm.md", "capm")],
    )
    result = _build_index(inv)
    assert "[[" in result
    assert "CAPM" in result


def test_build_index_empty_persona_section_omitted():
    inv = _make_inventory()  # all empty
    result = _build_index(inv)
    assert "## Persona Pages" not in result


def test_build_index_persona_included_when_present():
    inv = _make_inventory(
        persona=[_make_entry("Prof Deepa", "persona/deepa.md", "deepa")],
    )
    result = _build_index(inv)
    assert "## Persona Pages" in result
