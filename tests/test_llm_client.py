"""
tests/test_llm_client.py — Tests for webapp/api/llm_client.py

Avoids constructing LLMClient.__init__ (which imports and connects to real SDKs).
Uses object.__new__ + manual attribute assignment for append_* method tests.
"""

import json
from unittest.mock import MagicMock

import pytest

from llm_client import (
    LLMClient,
    NormalizedResponse,
    ToolCall,
    _is_reasoning_unsupported,
)


# ---------------------------------------------------------------------------
# _is_reasoning_unsupported (module-level function)
# ---------------------------------------------------------------------------

_UNSUPPORTED_MESSAGES = [
    "reasoning_effort is not supported",
    "unsupported parameter: reasoning_effort",
    "unknown field 'reasoning_effort'",
    "extra inputs are not permitted",
    "invalid_request_error occurred",
]


@pytest.mark.parametrize("msg", _UNSUPPORTED_MESSAGES)
def test_is_reasoning_unsupported_matches(msg):
    assert _is_reasoning_unsupported(Exception(msg)) is True


def test_is_reasoning_unsupported_regular_error():
    assert _is_reasoning_unsupported(Exception("network timeout")) is False


# ---------------------------------------------------------------------------
# LLMClient._to_nebius_tools (static method — no __init__ needed)
# ---------------------------------------------------------------------------

def test_to_nebius_tools_empty():
    assert LLMClient._to_nebius_tools([]) == []


def test_to_nebius_tools_single():
    claude_tool = {
        "name": "read_page",
        "description": "Read a wiki page",
        "input_schema": {"type": "object", "properties": {"slug": {"type": "string"}}},
    }
    result = LLMClient._to_nebius_tools([claude_tool])
    assert len(result) == 1
    t = result[0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "read_page"
    assert t["function"]["description"] == "Read a wiki page"
    assert "parameters" in t["function"]


def test_to_nebius_tools_multiple():
    tools = [
        {"name": f"tool_{i}", "description": f"desc {i}", "input_schema": {}}
        for i in range(3)
    ]
    result = LLMClient._to_nebius_tools(tools)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# append_assistant_turn — claude provider
# ---------------------------------------------------------------------------

def _make_client(provider: str) -> LLMClient:
    """Construct LLMClient bypassing __init__ to avoid real SDK imports."""
    client = object.__new__(LLMClient)
    client.provider = provider
    client.model    = "test-model"
    client._claude  = MagicMock()
    client._openai  = MagicMock()
    client._reasoning_supported = None
    return client


def test_append_assistant_turn_claude():
    client   = _make_client("claude")
    messages = []
    raw      = MagicMock()
    raw.content = ["block1", "block2"]
    response = NormalizedResponse(stop_reason="end_turn", tool_calls=[], text="hi", raw=raw)
    client.append_assistant_turn(messages, response)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == raw.content


def test_append_assistant_turn_nebius_no_tools():
    client   = _make_client("nebius")
    messages = []
    response = NormalizedResponse(stop_reason="end_turn", tool_calls=[], text="answer text", raw=None)
    client.append_assistant_turn(messages, response)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"


def test_append_assistant_turn_nebius_with_tools():
    client   = _make_client("nebius")
    messages = []
    tc       = ToolCall(id="tc1", name="read_page", input={"slug": "capm"})
    response = NormalizedResponse(stop_reason="tool_use", tool_calls=[tc], text="", raw=None)
    client.append_assistant_turn(messages, response)
    assert len(messages) == 1
    msg = messages[0]
    assert msg["role"] == "assistant"
    # tool_calls field must be present with correct format
    assert "tool_calls" in msg


# ---------------------------------------------------------------------------
# append_tool_results
# ---------------------------------------------------------------------------

def test_append_tool_results_claude_format():
    client   = _make_client("claude")
    messages = []
    tc       = ToolCall(id="tc1", name="read_page", input={"slug": "capm"})
    results  = ['{"slug": "capm", "title": "CAPM"}']
    client.append_tool_results(messages, [tc], results)
    # Claude format: single user message with tool_result list
    assert len(messages) == 1
    msg = messages[0]
    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "tool_result"


def test_append_tool_results_nebius_format():
    client   = _make_client("nebius")
    messages = []
    tc       = ToolCall(id="tc1", name="read_page", input={"slug": "capm"})
    results  = ['{"result": "ok"}']
    client.append_tool_results(messages, [tc], results)
    # Nebius format: one role="tool" message per result
    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
