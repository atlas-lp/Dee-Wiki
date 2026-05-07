"""
tests/test_rag.py — Tests for webapp/api/rag.py

Pre-seeds sys.modules["kb"] before importing rag so the bare-name import
`from kb import EMBED_MODEL, QUERY_PREFIX` works without the real kb.
"""

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub kb before rag is imported
# ---------------------------------------------------------------------------

_kb_mock = MagicMock()
_kb_mock.EMBED_MODEL  = "test-model"
_kb_mock.QUERY_PREFIX = "test: "
sys.modules["kb"] = _kb_mock

import rag
from rag import _extract_json, do_rag_search


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json('{"key": "val"}') == {"key": "val"}


def test_extract_json_fenced():
    result = _extract_json('```json\n{"a": 1}\n```')
    assert result == {"a": 1}


def test_extract_json_embedded():
    result = _extract_json('some preamble {"x": 99} trailing')
    assert isinstance(result, dict)
    assert result.get("x") == 99


def test_extract_json_no_json():
    assert _extract_json("no json here") == {}


def test_extract_json_empty():
    assert _extract_json("") == {}


def test_extract_json_nested():
    text = '{"a": {"b": [1, 2]}}'
    result = _extract_json(text)
    assert result == {"a": {"b": [1, 2]}}


# ---------------------------------------------------------------------------
# do_rag_search
# ---------------------------------------------------------------------------

def test_do_rag_search_no_index(sample_chunks):
    results = do_rag_search("query", sample_chunks, faiss_index=None)
    assert results == []


def test_do_rag_search_empty_chunks():
    mock_index = MagicMock()
    results = do_rag_search("query", [], faiss_index=mock_index)
    assert results == []


def test_do_rag_search_embedding_failure(sample_chunks):
    mock_index = MagicMock()
    mock_index.ntotal = len(sample_chunks)
    with patch("rag._get_query_embedding", return_value=None):
        results = do_rag_search("query", sample_chunks, faiss_index=mock_index)
    assert results == []


def test_do_rag_search_returns_top_k(sample_chunks):
    mock_index = MagicMock()
    mock_index.ntotal = len(sample_chunks)
    n = len(sample_chunks)
    # Return indices 0..n-1 with descending scores
    mock_index.search.return_value = (
        np.array([[0.9, 0.8, 0.7]], dtype=np.float32),
        np.array([[0, 1, 2]],       dtype=np.int64),
    )
    emb = np.ones(3072, dtype=np.float32)

    with patch("rag._get_query_embedding", return_value=emb):
        results = do_rag_search("query", sample_chunks, faiss_index=mock_index, top_k=3)

    assert len(results) == 3


def test_do_rag_search_result_schema(sample_chunks):
    mock_index = MagicMock()
    mock_index.ntotal = 3
    mock_index.search.return_value = (
        np.array([[0.9]], dtype=np.float32),
        np.array([[0]],   dtype=np.int64),
    )
    emb = np.ones(3072, dtype=np.float32)

    with patch("rag._get_query_embedding", return_value=emb):
        results = do_rag_search("query", sample_chunks, faiss_index=mock_index, top_k=1)

    assert len(results) == 1
    r = results[0]
    for key in ("source", "content", "score", "bloom_level"):
        assert key in r


def test_do_rag_search_bloom_filter(sample_chunks):
    # sample_chunks has: [Remember=0, Understand=1, Create=2]
    # bloom_level="Understand" (order=2) should exclude Create (order=6)
    mock_index = MagicMock()
    mock_index.ntotal = 3
    mock_index.search.return_value = (
        np.array([[0.9, 0.8, 0.7]], dtype=np.float32),
        np.array([[0, 1, 2]],       dtype=np.int64),
    )
    emb = np.ones(3072, dtype=np.float32)

    with patch("rag._get_query_embedding", return_value=emb):
        results = do_rag_search(
            "query", sample_chunks, faiss_index=mock_index,
            top_k=5, bloom_level="Understand",
        )

    bloom_levels = [r["bloom_level"] for r in results]
    assert "Create" not in bloom_levels
