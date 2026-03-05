"""System context for the voice intent agent.

Registry-based discovery of all queryable data sources and executable actions.
Auto-generates LLM tool definitions and system prompt context from registrations.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from home_agent.core.logging import get_logger

_log = get_logger(service="voice_system_context")

QueryFn = Callable[[], Awaitable[str]]
ActionFn = Callable[[], Awaitable[None]]
ContextFn = Callable[[], str]


@dataclass
class QueryRegistration:
    name: str
    description: str
    query_fn: QueryFn
    context_fn: Optional[ContextFn] = None


@dataclass
class ActionRegistration:
    name: str
    description: str
    handler: ActionFn
    confirmation: str
    category: str = "general"
    parameters: Optional[Dict[str, Any]] = None


class SystemContext:
    """Registry of all queryable sources and executable actions.

    Tool definitions, system prompt context, and dispatch routing are all
    generated dynamically from the registries. Adding a new capability
    is a single register_query() or register_action() call.
    """

    def __init__(self) -> None:
        self._queries: Dict[str, QueryRegistration] = {}
        self._actions: Dict[str, ActionRegistration] = {}
        self._dynamic_context: Dict[str, str] = {}
        self._mqtt_data: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_query(
        self,
        name: str,
        description: str,
        query_fn: QueryFn,
        context_fn: Optional[ContextFn] = None,
    ) -> None:
        self._queries[name] = QueryRegistration(
            name=name, description=description,
            query_fn=query_fn, context_fn=context_fn,
        )
        _log.info("query_registered", name=name)

    def register_action(
        self,
        name: str,
        description: str,
        handler: ActionFn,
        confirmation: str,
        category: str = "general",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._actions[name] = ActionRegistration(
            name=name, description=description,
            handler=handler, confirmation=confirmation,
            category=category, parameters=parameters,
        )

    def unregister_actions_by_category(self, category: str) -> None:
        """Remove all actions in a category (used before re-registering from discovery)."""
        to_remove = [k for k, v in self._actions.items() if v.category == category]
        for k in to_remove:
            del self._actions[k]

    def update_dynamic_context(self, key: str, text: str) -> None:
        """Update a dynamic context section (e.g., from MQTT retained messages)."""
        self._dynamic_context[key] = text

    def store_mqtt_data(self, key: str, data: Any) -> None:
        """Store raw MQTT data for use by registrations."""
        self._mqtt_data[key] = data

    def get_mqtt_data(self, key: str) -> Any:
        return self._mqtt_data.get(key)

    # ------------------------------------------------------------------
    # Tool generation
    # ------------------------------------------------------------------

    def build_query_tool(self) -> Dict[str, Any]:
        """Generate the query_system tool definition from registered queries."""
        if not self._queries:
            return {}
        desc_parts = []
        for q in sorted(self._queries.values(), key=lambda x: x.name):
            desc_parts.append("%s (%s)" % (q.name, q.description))
        return {
            "type": "function",
            "function": {
                "name": "query_system",
                "description": "Query live system data. Available: " + ", ".join(desc_parts),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "enum": sorted(self._queries.keys()),
                            "description": "Which data source to query",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def build_action_tools(self) -> List[Dict[str, Any]]:
        """Generate tool definitions from registered actions.

        Groups actions by category into compound tools where it makes sense,
        or individual tools for actions with custom parameters.
        """
        tools = []
        by_category: Dict[str, List[ActionRegistration]] = {}
        for a in self._actions.values():
            if a.parameters:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": a.name,
                        "description": a.description,
                        "parameters": a.parameters,
                    },
                })
            else:
                by_category.setdefault(a.category, []).append(a)

        for cat, actions in sorted(by_category.items()):
            if len(actions) == 1:
                a = actions[0]
                tools.append({
                    "type": "function",
                    "function": {
                        "name": a.name,
                        "description": a.description,
                        "parameters": {"type": "object", "properties": {}},
                    },
                })
            else:
                names = [a.name for a in actions]
                descs = ["%s (%s)" % (a.name, a.description) for a in actions]
                tool_name = cat + "_command"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "Execute a %s command. Available: %s" % (cat, ", ".join(descs)),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "enum": names,
                                },
                            },
                            "required": ["command"],
                        },
                    },
                })
        return tools

    def build_all_tools(self) -> List[Dict[str, Any]]:
        """Build the complete tool list for the LLM."""
        tools = []
        qt = self.build_query_tool()
        if qt:
            tools.append(qt)
        tools.extend(self.build_action_tools())
        return tools

    # ------------------------------------------------------------------
    # System prompt context
    # ------------------------------------------------------------------

    def build_prompt_context(self) -> str:
        """Generate the system knowledge section for the LLM prompt."""
        sections = []

        # Static context from registered queries
        query_ctx = []
        for q in sorted(self._queries.values(), key=lambda x: x.name):
            if q.context_fn:
                try:
                    ctx = q.context_fn()
                    if ctx:
                        query_ctx.append(ctx)
                except Exception:
                    pass
        if query_ctx:
            sections.append("AVAILABLE DATA SOURCES:\n" + "\n".join("- " + c for c in query_ctx))

        # Action context
        action_ctx = []
        by_cat: Dict[str, List[str]] = {}
        for a in sorted(self._actions.values(), key=lambda x: x.name):
            by_cat.setdefault(a.category, []).append(a.description)
        for cat, descs in sorted(by_cat.items()):
            action_ctx.append("%s: %s" % (cat.title(), ", ".join(descs)))
        if action_ctx:
            sections.append("AVAILABLE ACTIONS:\n" + "\n".join("- " + c for c in action_ctx))

        # Dynamic context (from MQTT)
        for key in sorted(self._dynamic_context.keys()):
            val = self._dynamic_context[key]
            if val:
                sections.append(val)

        # MQTT architecture
        sections.append(
            "MQTT ARCHITECTURE:\n"
            "- Base topic: homeagent\n"
            "- Event envelope: {id, ts, source, type, trace_id, data}\n"
            "- Announce: homeagent/announce/request {text, targets?}\n"
            "- Mute: homeagent/announce/mute {muted_until_unix}\n"
            "- Lights: homeagent/lutron/command {action: on|off|level|scene, device_id?, level?, scene_name?}\n"
            "- Triggers: homeagent/time/cron/{type} {manual: true}"
        )

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute_query(self, name: str) -> str:
        """Route a query to the registered handler."""
        reg = self._queries.get(name)
        if not reg:
            return "Unknown query: %s" % name
        try:
            return await reg.query_fn()
        except Exception as e:
            _log.exception("query_failed", query=name)
            return "I was unable to get that information right now."

    async def execute_action(self, name: str) -> str:
        """Execute a registered action and return the confirmation text."""
        reg = self._actions.get(name)
        if not reg:
            return "Unknown action: %s" % name
        try:
            await reg.handler()
            return reg.confirmation
        except Exception as e:
            _log.exception("action_failed", action=name)
            return "I had trouble executing that action."

    def find_action(self, name: str) -> Optional[ActionRegistration]:
        return self._actions.get(name)

    def find_category_action(self, category_command: str, action_name: str) -> Optional[ActionRegistration]:
        """Find an action by its name within a category compound tool."""
        return self._actions.get(action_name)

    @property
    def query_names(self) -> List[str]:
        return sorted(self._queries.keys())

    @property
    def action_names(self) -> List[str]:
        return sorted(self._actions.keys())
