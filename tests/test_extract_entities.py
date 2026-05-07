"""
tests/test_extract_entities.py — Tests for scripts/extract_entities.py helpers.
"""

import pytest

from extract_entities import (
    chunk_hash,
    _normalize_rel_type,
    _fix_json,
    generate_wiki_page,
    RELATIONSHIP_TYPE_MAP,
)


# ---------------------------------------------------------------------------
# chunk_hash
# ---------------------------------------------------------------------------

def test_chunk_hash_stable():
    chunk = {"content": "hello world", "source": "a.md"}
    h1 = chunk_hash(chunk)
    h2 = chunk_hash(chunk)
    assert h1 == h2


def test_chunk_hash_hex_length():
    chunk = {"content": "test content here", "source": "b.md"}
    h = chunk_hash(chunk)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_chunk_hash_different_content():
    h1 = chunk_hash({"content": "foo"})
    h2 = chunk_hash({"content": "bar"})
    assert h1 != h2


# ---------------------------------------------------------------------------
# _normalize_rel_type
# ---------------------------------------------------------------------------

def test_normalize_rel_type_known_entries():
    # Spot-check 5 entries from RELATIONSHIP_TYPE_MAP
    samples = list(RELATIONSHIP_TYPE_MAP.items())[:5]
    for raw, expected in samples:
        assert _normalize_rel_type(raw) == expected


def test_normalize_rel_type_canonical_passthrough():
    assert _normalize_rel_type("uses") == "uses"


def test_normalize_rel_type_strips_whitespace():
    assert _normalize_rel_type("  uses  ") == "uses"


def test_normalize_rel_type_unknown_underscored():
    result = _normalize_rel_type("some novel relation")
    assert result == "some_novel_relation"


def test_normalize_rel_type_uppercase_lowered():
    # Should be lowercased before lookup
    result = _normalize_rel_type("IS A TYPE OF")
    assert result == "instance_of"


# ---------------------------------------------------------------------------
# _fix_json
# ---------------------------------------------------------------------------

def test_fix_json_valid():
    result = _fix_json('{"key": "val"}')
    assert result == {"key": "val"}


def test_fix_json_code_fences():
    text = '```json\n{"a": 1}\n```'
    result = _fix_json(text)
    assert result == {"a": 1}


def test_fix_json_embedded_in_text():
    text = 'Here is the result: {"x": 99} and more text.'
    result = _fix_json(text)
    assert isinstance(result, dict)
    assert result.get("x") == 99


def test_fix_json_invalid_returns_none():
    result = _fix_json("not json at all")
    assert result is None


def test_fix_json_empty_returns_none():
    result = _fix_json("")
    assert result is None


# ---------------------------------------------------------------------------
# generate_wiki_page
# ---------------------------------------------------------------------------

def _sample_item(with_relationships=True):
    item = {
        "slug": "discounted-cash-flow",
        "name": "Discounted Cash Flow",
        "description": "A valuation method that estimates the present value of future cash flows.",
        "tags": ["valuation", "corporate-finance"],
    }
    if with_relationships:
        item["relationships"] = [
            {"target": "capm",  "type": "uses"},
            {"target": "wacc",  "type": "depends_on"},
        ]
    else:
        item["relationships"] = []
    return item


def test_generate_wiki_page_starts_with_frontmatter():
    page = generate_wiki_page(_sample_item(), "concept", "Damodaran")
    assert page.startswith("---")


def test_generate_wiki_page_has_type():
    page = generate_wiki_page(_sample_item(), "concept", "Damodaran")
    assert "type: concept" in page


def test_generate_wiki_page_relationships_present():
    page = generate_wiki_page(_sample_item(with_relationships=True), "concept", "Damodaran")
    assert "capm"  in page
    assert "wacc"  in page


def test_generate_wiki_page_no_relationships_block_when_empty():
    page = generate_wiki_page(_sample_item(with_relationships=False), "concept", "Damodaran")
    assert "## Relationships" not in page


def test_generate_wiki_page_body_structure():
    page = generate_wiki_page(_sample_item(), "concept", "Damodaran")
    assert "# Discounted Cash Flow" in page
    assert "valuation method" in page.lower() or "present value" in page.lower()
    assert "Damodaran" in page
