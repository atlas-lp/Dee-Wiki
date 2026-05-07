"""
tests/test_export_for_web.py — Tests for scripts/export_for_web.py helpers.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import export_for_web


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------

def test_content_hash_stable():
    h1 = export_for_web.content_hash("hello world")
    h2 = export_for_web.content_hash("hello world")
    assert h1 == h2


def test_content_hash_sha256_hex():
    h = export_for_web.content_hash("test")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_content_hash_different_inputs():
    h1 = export_for_web.content_hash("foo")
    h2 = export_for_web.content_hash("bar")
    assert h1 != h2


def test_content_hash_unicode():
    # Multi-byte input should not raise
    h = export_for_web.content_hash("こんにちは")
    assert len(h) == 64


def test_content_hash_empty_string():
    h = export_for_web.content_hash("")
    assert len(h) == 64


# ---------------------------------------------------------------------------
# load_existing_chunks
# ---------------------------------------------------------------------------

def test_load_existing_chunks_missing_file(tmp_path):
    missing = tmp_path / "no_chunks.json"
    result, seen = export_for_web.load_existing_chunks(missing)
    assert result == []
    assert seen == set()


def test_load_existing_chunks_empty_array(tmp_path):
    f = tmp_path / "chunks.json"
    f.write_text("[]", encoding="utf-8")
    result, seen = export_for_web.load_existing_chunks(f)
    assert result == []
    assert seen == set()


def test_load_existing_chunks_records(tmp_path):
    records = [
        {"content": "alpha text", "source": "a.md"},
        {"content": "beta text",  "source": "b.md"},
    ]
    f = tmp_path / "chunks.json"
    f.write_text(json.dumps(records), encoding="utf-8")

    result, seen = export_for_web.load_existing_chunks(f)
    assert len(result) == 2
    # seen should contain hashes of both contents
    for r in records:
        assert export_for_web.content_hash(r["content"]) in seen


# ---------------------------------------------------------------------------
# load_chunks (source loader)
# ---------------------------------------------------------------------------

def test_load_chunks_missing_file(tmp_path):
    missing = tmp_path / "nonexistent.json"
    with patch("export_for_web.CHUNKS_FILE", missing):
        result = export_for_web.load_chunks()
    assert result == []


def test_load_chunks_sets_type_field(tmp_path):
    data = [{"content": "hello", "source": "x.md"}]
    f = tmp_path / "chunks.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    with patch("export_for_web.CHUNKS_FILE", f):
        result = export_for_web.load_chunks()
    assert result[0]["type"] == "rag"
