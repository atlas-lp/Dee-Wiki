"""
tests/test_chunk_blooms_tagger.py — Tests for scripts/chunk_blooms_tagger.py

Only pure functions are tested — torch/transformers are stubbed by conftest.py.
"""

import numpy as np
import pytest

from chunk_blooms_tagger import (
    BLOOMS_LABEL_NAMES,
    BLOOMS_TUNED_THRESHOLDS,
    _split_sentences,
    _probs_to_labels,
    _probs_to_bucket,
)


# ---------------------------------------------------------------------------
# _split_sentences
# ---------------------------------------------------------------------------

def test_split_sentences_basic():
    sents = _split_sentences("Cat sat. Dog ran.")
    assert len(sents) >= 1
    # should capture both clauses
    joined = " ".join(sents).lower()
    assert "cat" in joined or "dog" in joined


def test_split_sentences_empty():
    assert _split_sentences("") == []


def test_split_sentences_short_filtered():
    text = "Hi.\nThis is a longer sentence that should be included."
    sents = _split_sentences(text)
    # "Hi" is < 5 chars after stripping, so only the longer line survives
    assert any("longer" in s.lower() or "included" in s.lower() for s in sents)


def test_split_sentences_heading_colon_skipped():
    text = "Key Topics:\nThis is a real sentence."
    sents = _split_sentences(text)
    assert not any(s.strip().endswith(":") for s in sents)


def test_split_sentences_abbreviation_protected():
    # "Dr. Smith" should not cause a mid-sentence split
    sents = _split_sentences("Dr. Smith works at the university. He is a professor.")
    assert len(sents) <= 2  # must not produce 3+ fragments


def test_split_sentences_bullet_stripped():
    sents = _split_sentences("- First item here.\n• Second item here.")
    for s in sents:
        assert not s.startswith("-")
        assert not s.startswith("•")


def test_split_sentences_markdown_bold_stripped():
    sents = _split_sentences("**Important concept** is defined here.")
    for s in sents:
        assert "**" not in s


# ---------------------------------------------------------------------------
# _probs_to_labels
# ---------------------------------------------------------------------------

def test_probs_to_labels_all_above():
    probs = np.array([0.9, 0.9, 0.9, 0.9, 0.9, 0.9], dtype=np.float32)
    thresholds = {label: 0.1 for label in BLOOMS_LABEL_NAMES}
    labels = _probs_to_labels(probs, thresholds)
    assert set(labels) == set(BLOOMS_LABEL_NAMES)


def test_probs_to_labels_none_above():
    probs = np.zeros(6, dtype=np.float32)
    thresholds = {label: 0.5 for label in BLOOMS_LABEL_NAMES}
    labels = _probs_to_labels(probs, thresholds)
    assert labels == ["Below threshold"]


def test_probs_to_labels_selective():
    probs = np.zeros(6, dtype=np.float32)
    probs[3] = 0.9  # Analyze
    probs[5] = 0.9  # Create
    thresholds = {label: 0.5 for label in BLOOMS_LABEL_NAMES}
    labels = _probs_to_labels(probs, thresholds)
    assert "Analyze" in labels
    assert "Create"  in labels
    assert "Remember" not in labels


def test_probs_to_labels_order_preserved():
    probs = np.array([0.8, 0.0, 0.8, 0.0, 0.8, 0.0], dtype=np.float32)
    thresholds = {label: 0.5 for label in BLOOMS_LABEL_NAMES}
    labels = _probs_to_labels(probs, thresholds)
    indices = [BLOOMS_LABEL_NAMES.index(l) for l in labels]
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# _probs_to_bucket
# ---------------------------------------------------------------------------

def test_probs_to_bucket_understand_wins():
    probs = np.array([0.9, 0.9, 0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    result = _probs_to_bucket(probs)
    assert result["bloom_bucket"] == "understand_bkt"


def test_probs_to_bucket_analyze_wins():
    probs = np.array([0.1, 0.1, 0.9, 0.9, 0.1, 0.1], dtype=np.float32)
    result = _probs_to_bucket(probs)
    assert result["bloom_bucket"] == "analyze_bkt"


def test_probs_to_bucket_create_wins():
    probs = np.array([0.1, 0.1, 0.1, 0.1, 0.9, 0.9], dtype=np.float32)
    result = _probs_to_bucket(probs)
    assert result["bloom_bucket"] == "create_bkt"


def test_probs_to_bucket_keys_present():
    probs = np.ones(6, dtype=np.float32) * 0.5
    result = _probs_to_bucket(probs)
    for key in ("bloom_bucket", "understand_bkt_score", "analyze_bkt_score", "create_bkt_score"):
        assert key in result
