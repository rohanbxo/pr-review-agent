"""Context hygiene: what the model sees on each call.

The graph state keeps the FULL conversation (it is what agent_steps and the trace record). Before
every model call it is projected through ``model_view``:

* the initial context brief -- PR metadata and every changed file's patch -- is never touched;
  that is the review material, not a lookup;
* the ``keep`` most recent tool results are passed in full;
* older tool results become a short stub naming the tool, its arguments and the result size, so
  the model knows it already looked and can call the tool again if it needs the content.

A 40k-token file read is dead weight three rounds later, and resending it on every round is where
most of the tokens went.

Prompt caching has to agree with this. Replacing an old result with a stub rewrites history, so a
single cache breakpoint at the end of the request never hits again once trimming starts: measured,
it costs MORE than no cache. Stubbing is monotonic, though -- a stub never changes back -- so the
prefix that ends at the newest stub is byte-identical in the next request. ``model_view`` therefore
puts explicit breakpoints on exactly two stable positions: the brief, and the newest stub. There is
no end-of-request breakpoint: the kept full results sit after a position that changes every round,
so caching them only pays the 1.25x write premium. Probe on OpenRouter -> anthropic/claude-haiku-4.5,
6 simulated rounds: brief + newest stub $0.164; + last message $0.184; top-level only $0.249;
uncached $0.224.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

CACHE_CONTROL = {"type": "ephemeral"}
_MAX_ARGS_CHARS = 300


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


def _with_breakpoint(message: BaseMessage) -> BaseMessage:
    block = {"type": "text", "text": _text(message.content), "cache_control": CACHE_CONTROL}
    return message.model_copy(update={"content": [block]})


def stub_text(name: str, args: dict | None, size_chars: int) -> str:
    args_json = json.dumps(args or {}, ensure_ascii=True, sort_keys=True)
    if len(args_json) > _MAX_ARGS_CHARS:
        args_json = args_json[:_MAX_ARGS_CHARS] + "...(truncated)"
    return (f"[Earlier tool result removed from context: {name}({args_json}) returned {size_chars:,} characters. "
            f"You already read it; call {name} again with the same arguments if you need the content.]")


def model_view(messages: list[BaseMessage], *, keep: int | None, cache: bool) -> list[BaseMessage]:
    """Project the full conversation onto what the model is sent. Pure; never mutates ``messages``.

    ``keep=None`` means trimming is off: the conversation is returned as-is with no block breakpoints
    (the caller uses the rolling top-level cache instead)."""
    if keep is None:
        return list(messages)
    if keep < 0:
        raise ValueError("keep must be >= 0")
    calls: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls[tc["id"]] = tc

    tool_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    to_stub = set(tool_positions[:-keep] if keep else tool_positions)
    brief = next((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), None)

    out: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if i in to_stub:
            tc = calls.get(m.tool_call_id) or {}
            name = m.name or tc.get("name") or "tool"
            m = ToolMessage(content=stub_text(name, tc.get("args"), len(_text(m.content))),
                            tool_call_id=m.tool_call_id, name=m.name)
        out.append(m)

    if cache:
        if brief is not None:
            out[brief] = _with_breakpoint(out[brief])
        if to_stub:
            newest_stub = max(to_stub)
            out[newest_stub] = _with_breakpoint(out[newest_stub])
    return out
