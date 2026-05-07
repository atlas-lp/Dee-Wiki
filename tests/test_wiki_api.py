"""
tests/test_wiki_api.py — Tests for webapp/api/wiki.py

Pre-seeds sys.modules for kb, rag, llm_client before import so bare-name
imports resolve without real file I/O or API connections.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub dependencies before importing wiki
# ---------------------------------------------------------------------------

# kb stub
_kb_mock = MagicMock()
_kb_mock.PROJECT_ROOT   = MagicMock()
_kb_mock.DATA_DIR       = MagicMock()
_kb_mock.WIKI_DIR       = MagicMock()
_kb_mock.VAULT          = MagicMock()
_kb_mock.INDEX_MD_PATH  = MagicMock()
_kb_mock.LOG_MD_PATH    = MagicMock()
_kb_mock._GITHUB_BASE   = "webapp"
_kb_mock._WIKI_FAISS_CACHE = MagicMock()
_kb_mock._WIKI_FAISS_SLUGS = MagicMock()
_kb_mock.WikiSearchIndex   = MagicMock()
_kb_mock.KnowledgeBase     = MagicMock()
_kb_mock._load_graph       = MagicMock(return_value={"nodes": {}, "edges": []})
sys.modules["kb"] = _kb_mock

# rag stub (already set, but ensure _extract_json is available)
if "rag" not in sys.modules:
    _rag_mock = MagicMock()
    _rag_mock._extract_json = MagicMock(return_value={})
    sys.modules["rag"] = _rag_mock

# test_pipeline.py (collected before this file, P < W) sets sys.modules["wiki"]
# to a MagicMock. Pop it so Python re-imports the real wiki module here.
sys.modules.pop("wiki", None)

from wiki import tool_read_page, _push_batch_to_github  # noqa: E402


# ---------------------------------------------------------------------------
# tool_read_page
# ---------------------------------------------------------------------------

def _graph_with_edges(from_slug, to_slugs):
    return {
        "edges": [
            {"from": from_slug, "to": t, "type": "related_to"}
            for t in to_slugs
        ]
    }


def test_tool_read_page_found():
    pages = {
        "capm": {"title": "CAPM", "content": "The Capital Asset Pricing Model..."},
    }
    graph = _graph_with_edges("capm", ["wacc", "beta"])

    result = tool_read_page("capm", pages, graph)

    assert result["slug"]  == "capm"
    assert result["title"] == "CAPM"
    assert "content" in result
    assert len(result["related_pages"]) == 2


def test_tool_read_page_not_found():
    result = tool_read_page("nonexistent", {}, {"edges": []})
    assert "error" in result


def test_tool_read_page_no_outgoing_edges():
    pages = {"capm": {"title": "CAPM", "content": "content"}}
    graph = {"edges": [{"from": "wacc", "to": "capm", "type": "related_to"}]}  # incoming only
    result = tool_read_page("capm", pages, graph)
    assert result["related_pages"] == []


def test_tool_read_page_multiple_edges():
    pages = {"root": {"title": "Root", "content": "content"}}
    graph = _graph_with_edges("root", ["a", "b", "c"])
    result = tool_read_page("root", pages, graph)
    assert len(result["related_pages"]) == 3


# ---------------------------------------------------------------------------
# _push_batch_to_github
# ---------------------------------------------------------------------------

def test_push_batch_skips_without_token():
    """No HTTP calls when GITHUB_TOKEN is absent."""
    with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GITHUB_REPO": ""}):
        with patch("wiki.requests.get") as mock_get:
            _push_batch_to_github({"some/path.md": "content"}, "test commit")
            mock_get.assert_not_called()


def test_push_batch_makes_api_calls():
    """With credentials set, the function calls GitHub REST API."""
    import requests as _req

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.side_effect = [
        {"object": {"sha": "head_sha_123"}},           # GET refs/heads/main
        {"tree": {"sha": "base_tree_sha"}},             # GET commits/{sha}
        {"sha": "blob_sha"},                            # POST blobs
        {"sha": "new_tree_sha"},                        # POST trees
        {"sha": "new_commit_sha"},                      # POST commits
        {},                                              # PATCH refs/heads/main
    ]

    with patch.dict(os.environ, {"GITHUB_TOKEN": "tok", "GITHUB_REPO": "owner/repo"}):
        with patch("wiki.requests.get",   return_value=fake_resp) as mock_get, \
             patch("wiki.requests.post",  return_value=fake_resp) as mock_post, \
             patch("wiki.requests.patch", return_value=fake_resp) as mock_patch:
            _push_batch_to_github({"webapp/Vault/wiki/page.md": "content"}, "auto: update")
            # final PATCH to update the ref must be called
            mock_patch.assert_called_once()
