"""
tests/test_kb.py — Tests for webapp/api/kb.py helpers.

Pre-seeds sys.modules["graph"] before importing kb so the bare-name import
`from graph import parse_frontmatter, strip_frontmatter, WIKI_DIR, VAULT`
resolves without hitting the real scripts/graph.py path logic.
"""

import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub 'graph' before kb is imported
# ---------------------------------------------------------------------------

_graph_stub = MagicMock()
_graph_stub.WIKI_DIR  = Path("/fake/wiki")
_graph_stub.VAULT     = Path("/fake")
_graph_stub.parse_frontmatter = MagicMock(return_value={})
_graph_stub.strip_frontmatter = MagicMock(side_effect=lambda c: c)
sys.modules.setdefault("graph", _graph_stub)

# Now we can import kb-level helpers
# We import the module but use patches to avoid touching the real filesystem.
import kb
from kb import _build_wiki_search_text, _load_wiki_pages


# ---------------------------------------------------------------------------
# _build_wiki_search_text
# ---------------------------------------------------------------------------

def test_build_wiki_search_text_basic(sample_wiki_page):
    # Use the real strip_frontmatter from scripts/graph via conftest path setup
    # but patch it to return a predictable body
    with patch("kb.strip_frontmatter", return_value="# Test Concept\n\nThis is the body.\n\n## Relationships\n\n- link"):
        result = _build_wiki_search_text(sample_wiki_page)
    assert "Test Concept" in result
    # aliases and tags should be concatenated in
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_wiki_search_text_strips_relationships(sample_wiki_page):
    # sample_wiki_page["content"] already contains "## Relationships\n\n- link"
    # and the graph stub's strip_frontmatter returns content unchanged (lambda c: c),
    # so _build_wiki_search_text must strip the Relationships section itself.
    result = _build_wiki_search_text(sample_wiki_page)
    assert "## Relationships" not in result
    assert "This is the body" in result


def test_build_wiki_search_text_no_aliases(sample_wiki_page):
    sample_wiki_page["aliases"] = []
    with patch("kb.strip_frontmatter", return_value="# Title\n\nSome content.\n"):
        result = _build_wiki_search_text(sample_wiki_page)
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_wiki_search_text_returns_str(sample_wiki_page):
    with patch("kb.strip_frontmatter", return_value="# T\n\nContent.\n"):
        result = _build_wiki_search_text(sample_wiki_page)
    assert isinstance(result, str)


def test_build_wiki_search_text_includes_title(sample_wiki_page):
    with patch("kb.strip_frontmatter", return_value="# Test Concept\n\nContent.\n"):
        result = _build_wiki_search_text(sample_wiki_page)
    assert sample_wiki_page["title"] in result


# ---------------------------------------------------------------------------
# _load_wiki_pages (reads from disk via tmp_wiki_dir fixture)
# ---------------------------------------------------------------------------

def test_load_wiki_pages_from_disk(tmp_wiki_dir):
    # Use patch.object(kb, ...) so the real module object is patched, not whatever
    # sys.modules["kb"] happens to point to at run-time (may be a mock from
    # test_rag.py / test_wiki_api.py collected after this file).
    _graph_stub.WIKI_DIR = tmp_wiki_dir
    _graph_stub.VAULT    = tmp_wiki_dir.parent

    with patch.object(kb, "WIKI_DIR", tmp_wiki_dir), \
         patch.object(kb, "parse_frontmatter", return_value={"type": "concept", "aliases": [], "tags": []}), \
         patch.object(kb, "strip_frontmatter", side_effect=lambda c: c):
        pages = _load_wiki_pages()

    assert len(pages) >= 2
    slugs = {p["slug"] for p in pages}
    assert "alpha" in slugs
    assert "beta"  in slugs
    for p in pages:
        for key in ("slug", "title", "aliases", "tags", "content", "type"):
            assert key in p


def test_load_wiki_pages_skips_index_log(tmp_wiki_dir):
    # Add index.md and log.md — they should be skipped
    (tmp_wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_wiki_dir / "log.md").write_text("# Log\n",   encoding="utf-8")

    with patch("kb.WIKI_DIR", tmp_wiki_dir), \
         patch("kb.parse_frontmatter", return_value={}), \
         patch("kb.strip_frontmatter", side_effect=lambda c: c):
        pages = _load_wiki_pages()

    slugs = {p["slug"] for p in pages}
    assert "index" not in slugs
    assert "log"   not in slugs


def test_load_wiki_pages_missing_dir():
    with patch("kb.WIKI_DIR", Path("/nonexistent/path/wiki")):
        pages = _load_wiki_pages()
    assert pages == []
