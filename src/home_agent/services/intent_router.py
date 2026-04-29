"""Intent router -- dispatches classified intents to targeted handlers.

Each category gets its own handler with only the relevant tools (1-3 max),
which keeps Groq/Llama reliable. Categories that don't need an LLM tool call
are dispatched directly.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Coroutine, Dict, List, Optional

from home_agent.core.logging import get_logger
from home_agent.integrations.llm import LLMToolCall, LLMTextResponse
from home_agent.integrations.llm_router import LLMRouter
from home_agent.services.voice_system_context import SystemContext

_log = get_logger(service="intent_router")

_SPOKEN_SYSTEM = (
    "You are Higgins, a helpful home assistant for the Smith family in Lynchburg, Virginia. "
    "Format output for spoken audio: spell out all numbers, times, dates, currency, "
    "percentages, and units as words. No URLs, markdown, or special characters. Be concise."
)


async def route(
    *,
    category: str,
    text: str,
    room_name: str,
    ctx: SystemContext,
    fast_llm: LLMRouter,
    perplexity_llm: Optional[Any] = None,
    execute_tool_fn: Callable[..., Coroutine],
) -> Optional[str]:
    """Route a classified intent to the appropriate handler. Returns response text."""

    if category == "query":
        return await _handle_query(text, room_name, ctx, fast_llm)
    elif category == "lighting":
        return await _handle_lighting(text, room_name, ctx, fast_llm)
    elif category == "scene":
        return await _handle_scene(text, room_name, ctx, fast_llm)
    elif category == "household":
        return await _handle_household(text, room_name, ctx, fast_llm, execute_tool_fn)
    elif category == "announcement":
        return await _handle_announcement(text, room_name, ctx, fast_llm, execute_tool_fn)
    elif category == "mute":
        return await _handle_mute(text, room_name, ctx, fast_llm, execute_tool_fn)
    elif category == "briefing":
        return await _handle_briefing(text, room_name, ctx, fast_llm, execute_tool_fn)
    elif category == "conversation":
        return await _handle_conversation(text, room_name, fast_llm, perplexity_llm)
    else:
        return None


async def _call_with_tools(
    text: str, room_name: str, tools: List[Dict[str, Any]], llm: LLMRouter,
) -> "LLMToolCall | LLMTextResponse | list":
    """Make a targeted LLM call with a small tool set."""
    messages = [
        {"role": "system", "content": _SPOKEN_SYSTEM},
        {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
    ]
    return await llm.chat_with_tools(
        messages=messages, tools=tools, max_tokens=512, temperature=0.2,
    )


async def _handle_query(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
) -> Optional[str]:
    """Handle data queries with just the query_system tool."""
    qt = ctx.build_query_tool()
    if not qt:
        return "I don't have any data sources available right now."
    result = await _call_with_tools(text, room_name, [qt], llm)
    if isinstance(result, LLMToolCall) and result.name == "query_system":
        query = result.arguments.get("query", "")
        _log.info("query_dispatch", query=query)
        return await ctx.execute_query(query)
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't find that information."


async def _handle_lighting(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
) -> Optional[str]:
    """Handle lighting commands with lights_on/off/level tools."""
    tools = []
    for name in ("lights_on", "lights_off", "lights_level"):
        reg = ctx.find_action(name)
        if reg and reg.parameters:
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": reg.description,
                    "parameters": reg.parameters,
                },
            })
    if not tools:
        return "Lighting controls aren't available right now."

    context = ctx.build_prompt_context()
    system = _SPOKEN_SYSTEM + "\n\n" + context
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
    ]
    result = await llm.chat_with_tools(
        messages=messages, tools=tools, max_tokens=256, temperature=0.1,
    )
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't process that lighting command."


async def _handle_scene(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
) -> Optional[str]:
    """Handle scene activation with just the activate_scene tool."""
    reg = ctx.find_action("activate_scene")
    if not reg or not reg.parameters:
        return "Scene control isn't available right now."

    context = ctx.build_prompt_context()
    system = _SPOKEN_SYSTEM + "\n\n" + context
    tools = [{
        "type": "function",
        "function": {
            "name": "activate_scene",
            "description": reg.description,
            "parameters": reg.parameters,
        },
    }]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
    ]
    result = await llm.chat_with_tools(
        messages=messages, tools=tools, max_tokens=256, temperature=0.1,
    )
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't activate that scene."


async def _handle_household(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
    execute_tool_fn: Callable,
) -> Optional[str]:
    """Handle household commands by matching to registered actions."""
    actions = ctx.build_action_tools()
    household_tools = [t for t in actions if t["function"]["name"] == "household_command"]
    if not household_tools:
        return "No household commands are configured."

    result = await _call_with_tools(text, room_name, household_tools, llm)
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I didn't recognize that household command."


async def _handle_announcement(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
    execute_tool_fn: Callable,
) -> Optional[str]:
    """Handle announcements with just the announce tool."""
    reg = ctx.find_action("announce")
    if not reg or not reg.parameters:
        return "Announcements aren't available right now."
    tools = [{
        "type": "function",
        "function": {
            "name": "announce",
            "description": reg.description,
            "parameters": reg.parameters,
        },
    }]
    result = await _call_with_tools(text, room_name, tools, llm)
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't make that announcement."


async def _handle_mute(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
    execute_tool_fn: Callable,
) -> Optional[str]:
    """Handle mute/unmute with targeted tools."""
    tools = []
    for name in ("mute_announcements", "unmute_announcements"):
        reg = ctx.find_action(name)
        if reg:
            params = reg.parameters or {"type": "object", "properties": {}}
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": reg.description,
                    "parameters": params,
                },
            })
    if not tools:
        return "Mute controls aren't available."
    result = await _call_with_tools(text, room_name, tools, llm)
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't process that mute command."


async def _handle_briefing(
    text: str, room_name: str, ctx: SystemContext, llm: LLMRouter,
    execute_tool_fn: Callable,
) -> Optional[str]:
    """Handle briefing triggers with the briefing_command tool."""
    actions = ctx.build_action_tools()
    briefing_tools = [t for t in actions if t["function"]["name"] == "briefing_command"]
    if not briefing_tools:
        return "Briefing commands aren't available."
    result = await _call_with_tools(text, room_name, briefing_tools, llm)
    if isinstance(result, LLMToolCall):
        return result
    if isinstance(result, LLMTextResponse) and result.text:
        return result.text
    return "I couldn't trigger that briefing."


async def _handle_conversation(
    text: str, room_name: str, llm: LLMRouter,
    perplexity_llm: Optional[Any] = None,
) -> Optional[str]:
    """Handle general questions with plain chat (no tools)."""
    _local_keywords = {
        "weather", "temperature", "forecast", "wind", "lights", "scene",
        "camera", "sensor", "humidity", "ups", "battery", "briefing",
        "mute", "unmute", "announce", "time", "calendar", "schedule",
    }
    text_lower = text.lower()
    is_local = any(re.search(r"\b" + kw + r"\b", text_lower) for kw in _local_keywords)

    if perplexity_llm and not is_local:
        try:
            pplx_answer = await perplexity_llm.chat(
                system="You are a helpful assistant for a family in Lynchburg, Virginia. "
                       "Answer concisely in one to three sentences. Format for spoken audio: "
                       "spell out numbers, no URLs, no markdown.",
                user=text,
                max_tokens=256,
                temperature=0.2,
            )
            if pplx_answer and len(pplx_answer.strip()) > 10:
                answer = pplx_answer.strip()
                answer = re.sub(r"\[\d+\]", "", answer)
                answer = re.sub(r"\*\*", "", answer)
                answer = re.sub(r"\s{2,}", " ", answer).strip()
                _log.info("perplexity_answer", answer=answer[:80])
                return answer
        except Exception as e:
            _log.warning("perplexity_failed", error=str(e)[:100])

    result = await llm.chat(
        system=_SPOKEN_SYSTEM,
        user="[Room: %s] %s" % (room_name, text),
        max_tokens=256,
        temperature=0.3,
    )
    return result.text if result.text else None
