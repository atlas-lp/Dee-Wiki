"""
tests/test_ingest.py — Tests for scripts/ingest.py helper functions.

Only pure I/O helpers are tested here; the Gemini-calling cmd_* functions
require network access and are out of scope.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Import helpers with path patches so the module doesn't crash on missing dirs
# ---------------------------------------------------------------------------

def _import_ingest():
    import ingest
    return ingest


# ---------------------------------------------------------------------------
# get_already_ingested
# ---------------------------------------------------------------------------

def test_get_already_ingested_missing_file(tmp_path):
    ingest = _import_ingest()
    missing = tmp_path / "no_such_file.json"
    with patch.object(type(ingest.INGESTED_FILE), "exists", return_value=False):
        with patch("ingest.INGESTED_FILE", missing):
            result = ingest.get_already_ingested()
    assert result == {}


def test_get_already_ingested_valid(tmp_path):
    ingest = _import_ingest()
    data = {"foo.md": {"chunks": 5, "words": 1000}}
    ingested_file = tmp_path / "ingested.json"
    ingested_file.write_text(json.dumps(data), encoding="utf-8")

    with patch("ingest.INGESTED_FILE", ingested_file):
        result = ingest.get_already_ingested()
    assert result == data


def test_get_already_ingested_corrupted_json(tmp_path):
    ingest = _import_ingest()
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    with patch("ingest.INGESTED_FILE", bad_file):
        result = ingest.get_already_ingested()
    assert result == {}


# ---------------------------------------------------------------------------
# save_ingested / roundtrip
# ---------------------------------------------------------------------------

def test_save_ingested_roundtrip(tmp_path):
    ingest = _import_ingest()
    data = {"book.pdf": {"chunks": 10, "words": 5000, "timestamp": "2025-01-01"}}
    ingested_file = tmp_path / "ingested.json"

    with patch("ingest.INGESTED_FILE", ingested_file), \
         patch("ingest.DATA_DIR", tmp_path):
        ingest.save_ingested(data)
        result = ingest.get_already_ingested()
    assert result == data


# ---------------------------------------------------------------------------
# scan_raw_files
# ---------------------------------------------------------------------------

def test_scan_raw_files_finds_supported(tmp_path):
    ingest = _import_ingest()
    raw_dir = tmp_path / "Vault" / "raw"
    raw_dir.mkdir(parents=True)

    (raw_dir / "doc.md").write_text("# Hello", encoding="utf-8")
    (raw_dir / "paper.pdf").write_bytes(b"%PDF-1.4 test")
    (raw_dir / "notes.txt").write_text("plain text", encoding="utf-8")
    (raw_dir / "spreadsheet.docx").write_bytes(b"docx content")

    with patch("ingest.RAW_DIR", raw_dir):
        files = ingest.scan_raw_files()

    exts = {Path(f).suffix for f in files}
    assert ".md"   in exts
    assert ".pdf"  in exts
    assert ".txt"  in exts
    assert ".docx" not in exts


def test_scan_raw_files_empty_dir(tmp_path):
    ingest = _import_ingest()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    with patch("ingest.RAW_DIR", raw_dir):
        files = ingest.scan_raw_files()
    assert files == []


def test_scan_raw_files_sorted(tmp_path):
    ingest = _import_ingest()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    (raw_dir / "z_last.md").write_text("z", encoding="utf-8")
    (raw_dir / "a_first.md").write_text("a", encoding="utf-8")
    (raw_dir / "m_middle.md").write_text("m", encoding="utf-8")

    with patch("ingest.RAW_DIR", raw_dir):
        files = ingest.scan_raw_files()

    assert files == sorted(files)
