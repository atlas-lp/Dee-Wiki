"""
tests/test_gemma_goal_extractor.py — Tests for scripts/gemma_goal_extractor.py

load_gemma() is never called; llm is always a MagicMock.
"""

from unittest.mock import MagicMock

import pytest

from gemma_goal_extractor import extract_goals


def _make_llm(response_text: str):
    """Build a mock llm whose create_chat_completion returns the given text."""
    llm = MagicMock()
    llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": response_text}}]
    }
    return llm


# ---------------------------------------------------------------------------
# Normal extraction
# ---------------------------------------------------------------------------

def test_extract_goals_returns_list():
    llm = _make_llm("1. Understand the concept of X\n2. Apply Y to solve Z")
    goals = extract_goals("Some chunk text.", llm)
    assert isinstance(goals, list)
    assert len(goals) >= 1


def test_extract_goals_strips_numbering():
    llm = _make_llm("1. Understand risk management\n2. Apply hedging techniques")
    goals = extract_goals("text", llm)
    for g in goals:
        assert not g.startswith("1.")
        assert not g.startswith("2.")


def test_extract_goals_strips_bullets():
    llm = _make_llm("- Understand risk\n* Apply theory")
    goals = extract_goals("text", llm)
    for g in goals:
        assert not g.startswith("-")
        assert not g.startswith("*")


# ---------------------------------------------------------------------------
# Empty / NONE responses
# ---------------------------------------------------------------------------

def test_extract_goals_none_response():
    llm = _make_llm("NONE")
    goals = extract_goals("text", llm)
    assert goals == []


def test_extract_goals_none_lowercase():
    llm = _make_llm("none")
    goals = extract_goals("text", llm)
    assert goals == []


def test_extract_goals_empty_response():
    llm = _make_llm("")
    goals = extract_goals("text", llm)
    assert goals == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_extract_goals_inference_error():
    llm = MagicMock()
    llm.create_chat_completion.side_effect = RuntimeError("GPU OOM")
    goals = extract_goals("text", llm)
    assert goals == []


# ---------------------------------------------------------------------------
# Content filtering
# ---------------------------------------------------------------------------

def test_extract_goals_filters_short_items():
    # Items with <= 5 chars should be excluded
    llm = _make_llm("1. Hi\n2. Understand the full picture of market risk")
    goals = extract_goals("text", llm)
    assert not any(len(g) <= 5 for g in goals)
    assert any("market risk" in g.lower() or "understand" in g.lower() for g in goals)
