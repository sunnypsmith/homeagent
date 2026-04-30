"""Anthropic Claude client with agent loop support.

Uses the Anthropic SDK directly (not OpenAI-compatible) for proper
tool calling support. Includes an agent loop that cycles through
tool_use/tool_result turns until Claude produces a final text answer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from home_agent.integrations.llm import LLMToolCall, LLMTextResponse

_log = logging.getLogger(__name__)

ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[str]]


@dataclass
class AgentResult:
    """Result of an agent loop run."""
    text: str
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    turns: int = 0


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

    async def agent_loop(
        self,
        *,
        system: str,
        user: str,
        tools: List[Dict[str, Any]],
        execute_tool: ToolExecutor,
        max_tokens: int = 1024,
        temperature: Optional[float] = 0.3,
        max_turns: int = 5,
    ) -> AgentResult:
        """Run a multi-turn agent loop with tool calling.

        Sends the user message with tools, executes any tool_use blocks,
        feeds results back, and loops until Claude returns a final text
        answer or max_turns is reached.
        """
        anthropic_tools = [_convert_tool_openai_to_anthropic(t) for t in tools]
        messages: List[Dict[str, Any]] = [{"role": "user", "content": user}]
        all_tool_calls: List[LLMToolCall] = []
        all_tool_results: List[Dict[str, Any]] = []

        for turn in range(max_turns):
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

            tool_use_blocks = []
            text_parts: List[str] = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_use_blocks.append(block)
                elif block.type == "text" and block.text.strip():
                    text_parts.append(block.text.strip())

            if not tool_use_blocks:
                return AgentResult(
                    text="\n".join(text_parts),
                    tool_calls=all_tool_calls,
                    tool_results=all_tool_results,
                    turns=turn + 1,
                )

            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            tool_result_blocks = []
            for block in tool_use_blocks:
                tc = LLMToolCall(
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                )
                all_tool_calls.append(tc)
                _log.info("agent_tool_call turn=%d tool=%s args=%s", turn, block.name, block.input)
                try:
                    result_text = await execute_tool(block.name, tc.arguments)
                except Exception as e:
                    result_text = "Error: %s" % str(e)[:200]
                    _log.warning("agent_tool_error tool=%s error=%s", block.name, result_text)
                all_tool_results.append({"tool": block.name, "args": tc.arguments, "result": result_text})
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_result_blocks})

        final_text = "\n".join(text_parts) if text_parts else "I wasn't able to complete that request."
        return AgentResult(
            text=final_text,
            tool_calls=all_tool_calls,
            tool_results=all_tool_results,
            turns=max_turns,
        )


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
