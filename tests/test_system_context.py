"""Tests for the SystemContext registry."""
from __future__ import annotations

import pytest

from home_agent.services.voice_system_context import SystemContext


# ---- Registration ----

def test_register_query_adds_to_registry(ctx: SystemContext) -> None:
    async def _q():
        return "ok"

    ctx.register_query("test_q", "A test query", _q)
    assert "test_q" in ctx.query_names


def test_register_action_adds_to_registry(ctx: SystemContext) -> None:
    async def _a():
        pass

    ctx.register_action("test_a", "A test action", _a, confirmation="Done.")
    assert "test_a" in ctx.action_names


# ---- Tool generation ----

def test_build_query_tool_format(ctx: SystemContext) -> None:
    async def _q():
        return ""

    ctx.register_query("alpha", "Alpha query", _q)
    ctx.register_query("beta", "Beta query", _q)

    tool = ctx.build_query_tool()
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == "query_system"
    enum = fn["parameters"]["properties"]["query"]["enum"]
    assert enum == ["alpha", "beta"]


def test_build_action_tools_grouped_by_category(ctx: SystemContext) -> None:
    async def _h():
        pass

    ctx.register_action("a1", "Action one", _h, confirmation="ok", category="house")
    ctx.register_action("a2", "Action two", _h, confirmation="ok", category="house")

    tools = ctx.build_action_tools()
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "house_command"
    enum = tools[0]["function"]["parameters"]["properties"]["command"]["enum"]
    assert set(enum) == {"a1", "a2"}


def test_build_action_tools_single_in_category(ctx: SystemContext) -> None:
    async def _h():
        pass

    ctx.register_action("solo", "Only action", _h, confirmation="ok", category="misc")
    tools = ctx.build_action_tools()
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "solo"


def test_build_action_tools_with_parameters(ctx: SystemContext) -> None:
    async def _h():
        pass

    params = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    ctx.register_action("custom", "Custom", _h, confirmation="ok", parameters=params)
    tools = ctx.build_action_tools()
    assert len(tools) == 1
    assert tools[0]["function"]["parameters"] == params


def test_build_all_tools_includes_queries_and_actions(ctx: SystemContext) -> None:
    async def _q():
        return ""

    async def _a():
        pass

    ctx.register_query("q1", "Query", _q)
    ctx.register_action("a1", "Action", _a, confirmation="ok")

    tools = ctx.build_all_tools()
    names = [t["function"]["name"] for t in tools]
    assert "query_system" in names
    assert "a1" in names


# ---- Prompt context ----

def test_build_prompt_context_includes_context_fn_output(ctx: SystemContext) -> None:
    async def _q():
        return ""

    ctx.register_query("q1", "Desc", _q, context_fn=lambda: "my_context_output")
    prompt = ctx.build_prompt_context()
    assert "my_context_output" in prompt


def test_build_prompt_context_includes_dynamic_context(ctx: SystemContext) -> None:
    ctx.update_dynamic_context("test_key", "DYNAMIC VALUE HERE")
    prompt = ctx.build_prompt_context()
    assert "DYNAMIC VALUE HERE" in prompt


# ---- Execution ----

@pytest.mark.asyncio
async def test_execute_query_routes_correctly(ctx: SystemContext) -> None:
    async def _q():
        return "the answer"

    ctx.register_query("my_q", "desc", _q)
    result = await ctx.execute_query("my_q")
    assert result == "the answer"


@pytest.mark.asyncio
async def test_execute_action_routes_and_returns_confirmation(ctx: SystemContext) -> None:
    called = []

    async def _handler():
        called.append(True)

    ctx.register_action("do_it", "desc", _handler, confirmation="All done!")
    result = await ctx.execute_action("do_it")
    assert result == "All done!"
    assert called == [True]


@pytest.mark.asyncio
async def test_execute_query_unknown_returns_error(ctx: SystemContext) -> None:
    result = await ctx.execute_query("nonexistent")
    assert "Unknown query" in result


@pytest.mark.asyncio
async def test_execute_action_unknown_returns_error(ctx: SystemContext) -> None:
    result = await ctx.execute_action("nonexistent")
    assert "Unknown action" in result


# ---- Unregister by category ----

def test_unregister_actions_by_category(ctx: SystemContext) -> None:
    async def _h():
        pass

    ctx.register_action("a1", "desc", _h, confirmation="ok", category="lights")
    ctx.register_action("a2", "desc", _h, confirmation="ok", category="lights")
    ctx.register_action("a3", "desc", _h, confirmation="ok", category="sonos")

    assert len(ctx.action_names) == 3
    ctx.unregister_actions_by_category("lights")
    assert ctx.action_names == ["a3"]


# ---- Dynamic context & MQTT data ----

def test_update_dynamic_context(ctx: SystemContext) -> None:
    ctx.update_dynamic_context("key1", "value1")
    assert "value1" in ctx.build_prompt_context()


def test_store_and_get_mqtt_data(ctx: SystemContext) -> None:
    ctx.store_mqtt_data("health", {"svc1": {"status": "ok"}})
    assert ctx.get_mqtt_data("health") == {"svc1": {"status": "ok"}}
    assert ctx.get_mqtt_data("missing") is None
