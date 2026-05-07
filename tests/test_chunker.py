"""
tests/test_chunker.py — Tests for scripts/chunker.py

chunk_text uses langchain_text_splitters (stubbed in conftest); we replace the
stub's RecursiveCharacterTextSplitter with a real implementation using the
langchain-text-splitters package if available, otherwise we test with a simple
fallback that splits on size.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Provide a real-enough RecursiveCharacterTextSplitter so chunk_text works
# ---------------------------------------------------------------------------

class _RealSplitter:
    def __init__(self, chunk_size=6000, chunk_overlap=300, length_function=len):
        self._size    = chunk_size
        self._overlap = chunk_overlap

    def split_text(self, text):
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self._size, len(text))
            chunks.append(text[start:end])
            if end == len(text):
                break
            start = end - self._overlap
        return chunks


_lts_mock = MagicMock()
_lts_mock.RecursiveCharacterTextSplitter = _RealSplitter
sys.modules["langchain_text_splitters"] = _lts_mock

from chunker import chunk_text, count_words  # noqa: E402


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_basic():
    text = "word " * 10_000  # ~50 000 chars — produces multiple chunks at 6000
    chunks = chunk_text(text, "src.md")
    assert len(chunks) > 1
    for c in chunks:
        assert "content"     in c
        assert "source"      in c
        assert "chunk_index" in c


def test_chunk_text_small_text():
    chunks = chunk_text("short text here", "src.md")
    assert len(chunks) == 1
    assert chunks[0]["content"] == "short text here"


def test_chunk_text_chunk_id_format():
    chunks = chunk_text("A " * 500, "src.md")
    assert chunks[0]["id"] == "src_chunk_0000"


def test_chunk_text_overlap():
    # With overlap=300 the tail of chunk N and head of chunk N+1 share text
    text = "x" * 6500  # slightly bigger than chunk_size=6000
    chunks = chunk_text(text, "src.md", chunk_size=6000, chunk_overlap=300)
    if len(chunks) >= 2:
        tail = chunks[0]["content"][-300:]
        head = chunks[1]["content"][:300]
        assert tail == head


def test_chunk_text_empty_string():
    chunks = chunk_text("", "src.md")
    assert chunks == []


def test_chunk_text_whitespace_only():
    chunks = chunk_text("   \n  ", "src.md")
    assert chunks == []


def test_chunk_text_source_preserved():
    chunks = chunk_text("Hello world", "my/path/file.md")
    assert chunks[0]["source"] == "my/path/file.md"


# ---------------------------------------------------------------------------
# count_words
# ---------------------------------------------------------------------------

def test_count_words_markdown(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("hello world foo", encoding="utf-8")
    # count_words reads the file
    result = count_words(f)
    assert result == 3


def test_count_words_empty(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    assert count_words(f) == 0


def test_count_words_nonexistent():
    result = count_words(Path("/nonexistent/path/file.md"))
    assert result == 0
