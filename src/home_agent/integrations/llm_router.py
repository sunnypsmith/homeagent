from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from typing import Any, Dict, List

from home_agent.integrations.llm import LLMClient, LLMToolCall, LLMTextResponse


@dataclass(frozen=True)
class LLMReply:
    provider: str
    text: str


class LLMRouter:
    """
    Try providers in order (primary, fallback, ...).
    """

    def __init__(self, providers: Sequence[Tuple[str, LLMClient]]) -> None:
        self._providers = list(providers)

    async def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMReply:
        last_err: Optional[Exception] = None
        for name, client in self._providers:
            try:
                text = await client.chat(
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return LLMReply(provider=name, text=text)
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError("All LLM providers failed") from last_err

    async def chat_with_tools(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> "LLMToolCall | LLMTextResponse":
        last_err: Optional[Exception] = None
        for name, client in self._providers:
            try:
                result = await client.chat_with_tools(
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return result
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError("All LLM providers failed") from last_err

