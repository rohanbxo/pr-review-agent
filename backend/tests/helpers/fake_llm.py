"""A scripted fake chat model for offline agent tests.

Supports ``bind_tools`` (so the default ``BaseChatModel.with_structured_output`` works via
forced tool calling, exactly like ChatAnthropic's default ``function_calling`` method) and records
every message list it was invoked with.

The script is a callable ``script(call: FakeCall) -> AIMessage``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

_ids = itertools.count(1)


@dataclass
class FakeCall:
    messages: list[BaseMessage]
    tool_names: list[str]
    tool_choice: Any

    @property
    def structured(self) -> bool:
        """True when this is the synthesize call (only the ReviewResult schema bound)."""
        return self.tool_names == ["ReviewResult"]


def tool_call(name: str, args: dict | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": f"call_{next(_ids)}", "type": "tool_call"}


def ai(content: str = "", tool_calls: Sequence[dict] = (), tokens: tuple[int, int] = (100, 20)) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(tool_calls),
        usage_metadata={"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)},
    )


def review(result: dict) -> AIMessage:
    """The synthesize answer: a forced ReviewResult tool call."""
    return ai(tool_calls=[tool_call("ReviewResult", result)], tokens=(300, 80))


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: Callable[[FakeCall], AIMessage]
    calls: list[FakeCall] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.bind(tool_names=names, tool_choice=tool_choice)

    def _generate(self, messages, stop=None, run_manager=None, tool_names=None, tool_choice=None, **kwargs):
        call = FakeCall(messages=list(messages), tool_names=list(tool_names or []), tool_choice=tool_choice)
        self.calls.append(call)
        msg = self.script(call)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def sequence_script(*responses: AIMessage | Callable[[FakeCall], AIMessage]) -> Callable[[FakeCall], AIMessage]:
    """Return responses in order (callables are called with the FakeCall)."""
    it = iter(responses)

    def _script(call: FakeCall) -> AIMessage:
        try:
            r = next(it)
        except StopIteration:  # pragma: no cover - test bug
            raise AssertionError(f"fake model called more times than scripted (tools={call.tool_names})")
        return r(call) if callable(r) else r

    return _script
