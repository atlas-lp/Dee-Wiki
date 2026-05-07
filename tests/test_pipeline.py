"""
tests/test_pipeline.py — Tests for webapp/api/pipeline.py

Pre-seeds sys.modules for kb, wiki, main_agent, llm_client before importing
pipeline so all bare-name imports resolve without real I/O.
"""

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub dependencies before importing pipeline
# ---------------------------------------------------------------------------

# Ensure kb is already stubbed (test_rag.py or test_wiki_api.py may have done it)
if "kb" not in sys.modules:
    _kb_mock = MagicMock()
    sys.modules["kb"] = _kb_mock
sys.modules["kb"].WIKI_DIR = MagicMock()

# stub wiki
_wiki_mock = MagicMock()
sys.modules["wiki"] = _wiki_mock

# stub main_agent
_main_agent_mock = MagicMock()
sys.modules["main_agent"] = _main_agent_mock

# llm_client already importable from conftest path setup

import pipeline
from pipeline import _pipeline_setup, query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kb(wiki_pages, slugs_in_pages):
    kb = MagicMock()
    kb._lock = threading.Lock()
    kb.wiki_pages   = wiki_pages
    kb.chunks       = []
    kb.faiss_index  = None
    kb.graph        = {"nodes": {}, "edges": []}
    kb.wiki_search  = MagicMock()
    kb.wiki_search.pages      = wiki_pages
    kb.wiki_search.bm25       = MagicMock()
    kb.wiki_search.faiss_index = None
    return kb


# ---------------------------------------------------------------------------
# _pipeline_setup
# ---------------------------------------------------------------------------

def test_pipeline_setup_returns_tuple():
    pages = [{"slug": "foo", "title": "Foo", "content": "content"}]
    kb    = _make_kb(pages, {"foo"})

    _wiki_mock.run_wiki_llm.return_value = {
        "sufficient": True,
        "selected_slugs": ["foo"],
        "note": "",
    }

    result = _pipeline_setup("What is foo?", kb, MagicMock())
    selected, wiki_result, chunks, faiss_index = result

    assert isinstance(selected,    list)
    assert isinstance(wiki_result, dict)
    assert isinstance(chunks,      list)


def test_pipeline_setup_resolves_slugs():
    pages = [
        {"slug": "alpha", "title": "Alpha", "content": "a"},
        {"slug": "beta",  "title": "Beta",  "content": "b"},
    ]
    kb = _make_kb(pages, {"alpha", "beta"})

    _wiki_mock.run_wiki_llm.return_value = {
        "sufficient": True,
        "selected_slugs": ["alpha", "beta"],
    }

    selected, *_ = _pipeline_setup("query", kb, MagicMock())
    slugs = {p["slug"] for p in selected}
    assert "alpha" in slugs
    assert "beta"  in slugs


def test_pipeline_setup_skips_unknown_slugs():
    pages = [{"slug": "alpha", "title": "Alpha", "content": "a"}]
    kb    = _make_kb(pages, {"alpha"})

    _wiki_mock.run_wiki_llm.return_value = {
        "sufficient": False,
        "selected_slugs": ["unknown-slug"],
    }

    selected, *_ = _pipeline_setup("query", kb, MagicMock())
    assert selected == []


def test_pipeline_setup_empty_slugs():
    kb = _make_kb([], set())
    _wiki_mock.run_wiki_llm.return_value = {
        "sufficient": False,
        "selected_slugs": [],
    }

    selected, *_ = _pipeline_setup("query", kb, MagicMock())
    assert selected == []


def test_pipeline_setup_calls_wiki_llm_once():
    kb = _make_kb([], set())
    _wiki_mock.run_wiki_llm.return_value = {"sufficient": False, "selected_slugs": []}
    _wiki_mock.run_wiki_llm.reset_mock()

    _pipeline_setup("query", kb, MagicMock())
    assert _wiki_mock.run_wiki_llm.call_count == 1


# ---------------------------------------------------------------------------
# query (blocking wrapper)
# ---------------------------------------------------------------------------

def test_query_collects_stream():
    def _mock_streaming(*args, **kwargs):
        yield ("text", "Hello ")
        yield ("text", "world.")
        yield ("done", {
            "sources":            {"wiki": ["foo"], "rag": []},
            "new_synthesis":      "Synthesis text",
            "should_wiki_update": False,
            "rag_chunks":         [],
        })

    kb = _make_kb([], set())

    with patch("pipeline.query_streaming", side_effect=_mock_streaming):
        result = query("test query", kb, MagicMock(), MagicMock())

    assert result["answer"] == "Hello world."
    assert "sources"            in result
    assert "new_synthesis"      in result
    assert "should_wiki_update" in result
