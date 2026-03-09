"""Anthropic Claude client for reasoning tasks.

Uses the Anthropic SDK directly (not OpenAI-compatible) for proper
tool calling support. Returns the same LLMToolCall/LLMTextResponse
types as the OpenAI client for compatibility.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from home_agent.integrations.llm import LLMToolCall, LLMTextResponse


class AnthropicClient:
    """Claude client with native tool calling support."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: Optional[float] = 0.3,
    ) -> str:
        """Simple chat without tools. Returns text."""
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        response = await self._client.messages.create(**kwargs)
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    async def chat_with_tools(
        self,
        *,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 1024,
        temperature: Optional[float] = 0.3,
    ) -> "LLMToolCall | LLMTextResponse | List[LLMToolCall]":
        """Chat with tool calling. Returns LLMToolCall(s) or LLMTextResponse.

        Tools should be in OpenAI format -- this method converts them to
        Anthropic format automatically.
        """
        anthropic_tools = [_convert_tool_openai_to_anthropic(t) for t in tools]

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": anthropic_tools,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        response = await self._client.messages.create(**kwargs)

        tool_calls: List[LLMToolCall] = []
        text_parts: List[str] = []

        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(LLMToolCall(
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))
            elif block.type == "text":
                if block.text.strip():
                    text_parts.append(block.text.strip())

        if tool_calls:
            return tool_calls if len(tool_calls) > 1 else tool_calls[0]
        return LLMTextResponse(text="\n".join(text_parts))


def _convert_tool_openai_to_anthropic(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert OpenAI tool format to Anthropic tool format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    fn = tool.get("function", tool)
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }
