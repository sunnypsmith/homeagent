"""Tests for LLM types and Anthropic tool conversion."""
from __future__ import annotations

from home_agent.integrations.llm import LLMToolCall, LLMTextResponse
from home_agent.integrations.llm_anthropic import _convert_tool_openai_to_anthropic


def test_llm_tool_call_dataclass() -> None:
    tc = LLMToolCall(name="do_thing", arguments={"key": "val"})
    assert tc.name == "do_thing"
    assert tc.arguments == {"key": "val"}


def test_llm_text_response_dataclass() -> None:
    tr = LLMTextResponse(text="Hello world")
    assert tr.text == "Hello world"


def test_convert_tool_openai_to_anthropic_full() -> None:
    openai_tool = {
        "type": "function",
        "function": {
            "name": "query_system",
            "description": "Query live data",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "enum": ["time", "weather"]},
                },
                "required": ["query"],
            },
        },
    }
    result = _convert_tool_openai_to_anthropic(openai_tool)
    assert result["name"] == "query_system"
    assert result["description"] == "Query live data"
    assert result["input_schema"]["type"] == "object"
    assert "query" in result["input_schema"]["properties"]


def test_convert_tool_openai_to_anthropic_minimal() -> None:
    openai_tool = {
        "type": "function",
        "function": {
            "name": "simple_action",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    result = _convert_tool_openai_to_anthropic(openai_tool)
    assert result["name"] == "simple_action"
    assert result["description"] == ""
    assert result["input_schema"] == {"type": "object", "properties": {}}


def test_convert_tool_openai_to_anthropic_no_function_wrapper() -> None:
    """When the tool dict doesn't have a 'function' key, fallback to top-level."""
    tool = {
        "name": "direct_tool",
        "description": "Direct",
        "parameters": {"type": "object", "properties": {}},
    }
    result = _convert_tool_openai_to_anthropic(tool)
    assert result["name"] == "direct_tool"
