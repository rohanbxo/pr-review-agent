"""The review agent.

    fetch_context ──► analyze ⇄ tools ──► synthesize ──► END

* ``fetch_context`` is deterministic Python: PR metadata + changed files are always needed, so
  fetching them in code saves two round trips and grounds the first LLM call.
* ``analyze`` is the only node with the review tools bound. Tool rounds are capped at
  ``settings.llm_max_tool_rounds``.
* ``synthesize`` is a separate node with NO review tools bound; it asks for structured output
  validated against ``ReviewResult`` (one repair retry), then drops findings on files the PR did
  not change.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph, add_messages
from pydantic import ValidationError

from app.agent.github_client import CallRecord, ReadOnlyGitHubClient
from app.agent.context import model_view
from app.agent.llm import structured_output
from app.agent.prompts import (
    REPAIR_INSTRUCTION,
    SYNTHESIZE_INSTRUCTION,
    SYSTEM_PROMPT,
    wrap_untrusted,
)
from app.agent.schema import ReviewResult
from app.agent.tools import PRRef, build_tools, fetch_changed_files, fetch_pull_request, render_files, summarize_pr
from app.config import get_settings

__all__ = ["StepEvent", "ReviewOutcome", "SynthesisError", "build_graph", "review_pull_request"]

_EMPTY_USAGE: dict[str, Any] = {
    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    # input_tokens INCLUDES cached tokens; these break it down (0 when the provider reports none).
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    # As reported by the provider in the response (OpenRouter usage.cost); 0.0 if not reported.
    "cost_usd": 0.0,
}


class SynthesisError(RuntimeError):
    """synthesize could not produce a valid ReviewResult after the repair retry."""

    def __init__(self, message: str, *, parse_failures: int = 0, attempts: int = 0) -> None:
        super().__init__(message)
        self.parse_failures = parse_failures
        self.attempts = attempts


@dataclass
class StepEvent:
    name: str
    input: dict | None
    output: dict | None
    latency_ms: int


@dataclass
class ReviewOutcome:
    result: ReviewResult
    usage: dict[str, int]
    calls: list[CallRecord] = field(default_factory=list)
    duration_s: float = 0.0
    dropped_findings: list[dict] = field(default_factory=list)
    # Structured-output attempts in synthesize that did not yield a valid ReviewResult (0 = first
    # try parsed; 1 = repaired on retry). A run that never parses raises SynthesisError instead.
    parse_failures: int = 0
    synthesis_attempts: int = 0


def _merge_usage(a: dict | None, b: dict | None) -> dict:
    out = dict(_EMPTY_USAGE)
    for src in (a or {}, b or {}):
        for k in out:
            if k == "cost_usd":
                out[k] = round(out[k] + float(src.get(k) or 0.0), 8)
            else:
                out[k] += int(src.get(k) or 0)
    return out


def usage_of(msg: Any) -> dict[str, Any]:
    um = getattr(msg, "usage_metadata", None) or {}
    details = um.get("input_token_details") or {}
    token_usage = (getattr(msg, "response_metadata", None) or {}).get("token_usage") or {}
    return {
        "input_tokens": int(um.get("input_tokens") or 0),
        "output_tokens": int(um.get("output_tokens") or 0),
        "total_tokens": int(um.get("total_tokens") or 0),
        "cache_read_input_tokens": int(details.get("cache_read") or 0),
        "cache_creation_input_tokens": int(details.get("cache_creation") or 0),
        "cost_usd": float(token_usage.get("cost") or 0.0),
    }


class ReviewState(TypedDict, total=False):
    repo: str
    pr_number: int
    messages: Annotated[list[AnyMessage], add_messages]
    pr: dict
    changed_files: list[str]
    tool_rounds: int
    usage: Annotated[dict, _merge_usage]
    result: dict
    dropped_findings: list[dict]
    parse_failures: int
    synthesis_attempts: int


def _close_dangling_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """If the tool budget ran out mid-request, answer the unanswered calls so the transcript is valid."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out: list[BaseMessage] = []
    for m in messages:
        out.append(m)
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                if tc["id"] not in answered:
                    out.append(ToolMessage(
                        content="Not executed: the tool-call budget for this review is exhausted.",
                        tool_call_id=tc["id"], name=tc["name"],
                    ))
    return out


def build_graph(*, client: ReadOnlyGitHubClient, llm: BaseChatModel, max_tool_rounds: int | None = None):
    max_rounds = max_tool_rounds if max_tool_rounds is not None else get_settings().llm_max_tool_rounds
    pr_ref = PRRef(repo="", pr_number=0)
    tools = build_tools(client, pr_ref)
    tools_by_name = {t.name: t for t in tools}
    analyst = llm.bind_tools(tools)
    settings = get_settings()
    keep = settings.llm_keep_tool_results

    async def fetch_context(state: ReviewState) -> dict:
        pr_ref.repo, pr_ref.pr_number = state["repo"], state["pr_number"]
        pr = await fetch_pull_request(client, pr_ref)
        files = await fetch_changed_files(client, pr_ref)
        pr_ref.head_sha = (pr.get("head") or {}).get("sha")
        pr_ref.base_sha = (pr.get("base") or {}).get("sha")
        pr_ref.changed_files = [f["filename"] for f in files if f.get("filename")]
        summary = summarize_pr(pr)
        context = (
            f"Review pull request #{state['pr_number']} in {state['repo']}.\n\n"
            "Pull request metadata:\n"
            + wrap_untrusted(json.dumps(summary, indent=2, ensure_ascii=False), source="pull_request")
            + f"\n\nChanged files ({len(files)}) with patches:\n"
            + wrap_untrusted(render_files(files, client.max_bytes), source="changed_files")
        )
        return {
            "pr": {k: v for k, v in summary.items() if k != "body"},
            "changed_files": pr_ref.changed_files,
            "tool_rounds": 0,
            "messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=context)],
        }

    async def analyze(state: ReviewState, config: RunnableConfig) -> dict:
        # The model sees a trimmed view; state keeps the full history (app/agent/context.py).
        view = model_view(state["messages"], keep=keep, cache=settings.llm_prompt_cache)
        response = await analyst.ainvoke(view, config)
        return {"messages": [response], "usage": usage_of(response)}

    async def run_tools(state: ReviewState, config: RunnableConfig) -> dict:
        last = state["messages"][-1]
        results: list[ToolMessage] = []
        for tc in getattr(last, "tool_calls", []) or []:
            tool = tools_by_name.get(tc["name"])
            if tool is None:
                content = f"ERROR: unknown tool {tc['name']!r}. Available: {', '.join(tools_by_name)}."
            else:
                try:
                    content = await tool.ainvoke(tc.get("args") or {}, config)
                except (ValidationError, TypeError, ValueError) as exc:
                    content = f"ERROR: invalid arguments for {tc['name']} ({type(exc).__name__})."
            results.append(ToolMessage(content=str(content), tool_call_id=tc["id"], name=tc["name"]))
        return {"messages": results, "tool_rounds": state.get("tool_rounds", 0) + 1}

    def route_after_analyze(state: ReviewState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("tool_rounds", 0) < max_rounds:
            return "tools"
        return "synthesize"

    async def synthesize(state: ReviewState, config: RunnableConfig) -> dict:
        structured = structured_output(llm, ReviewResult)
        # Same trimmed view as analyze, without cache breakpoints: synthesize binds a different
        # tool list, so it can never read the analyze cache (measured) and a write would be wasted.
        messages = model_view(_close_dangling_tool_calls(list(state["messages"])), keep=keep, cache=False)
        messages.append(HumanMessage(content=SYNTHESIZE_INSTRUCTION))
        usage = dict(_EMPTY_USAGE)
        parsed: ReviewResult | None = None
        error: str = ""
        failures = attempts = 0
        for attempt in range(2):  # first try + one repair retry
            attempts += 1
            out = await structured.ainvoke(messages, config)
            usage = _merge_usage(usage, usage_of(out.get("raw")))
            parsed = out.get("parsed")
            if isinstance(parsed, dict):
                try:
                    parsed = ReviewResult.model_validate(parsed)
                except ValidationError as exc:
                    parsed, out["parsing_error"] = None, exc
            if isinstance(parsed, ReviewResult):
                break
            failures += 1
            error = str(out.get("parsing_error") or "no ReviewResult object was returned")
            raw = out.get("raw")
            raw_text = raw.content if isinstance(getattr(raw, "content", None), str) else ""
            if attempt == 0:
                messages = messages + [HumanMessage(
                    content=REPAIR_INSTRUCTION.format(error=error[:2000])
                    + (f"\n\nYour previous text output was:\n{raw_text[:2000]}" if raw_text else "")
                )]
        if not isinstance(parsed, ReviewResult):
            raise SynthesisError(f"synthesize output failed validation after repair: {error[:500]}",
                                 parse_failures=failures, attempts=attempts)

        changed = set(state.get("changed_files") or [])
        kept, dropped = [], []
        for f in parsed.findings:
            (kept if f.file in changed else dropped).append(f)
        result = parsed.model_copy(update={
            "findings": kept,
            "files_reviewed": [p for p in parsed.files_reviewed if p in changed],
        })
        return {
            "result": result.model_dump(mode="json"),
            "dropped_findings": [f.model_dump(mode="json") for f in dropped],
            "usage": usage,
            "parse_failures": failures,
            "synthesis_attempts": attempts,
        }

    g = StateGraph(ReviewState)
    g.add_node("fetch_context", fetch_context)
    g.add_node("analyze", analyze)
    g.add_node("tools", run_tools)
    g.add_node("synthesize", synthesize)
    g.add_edge(START, "fetch_context")
    g.add_edge("fetch_context", "analyze")
    g.add_conditional_edges("analyze", route_after_analyze, {"tools": "tools", "synthesize": "synthesize"})
    g.add_edge("tools", "analyze")
    g.add_edge("synthesize", END)
    return g.compile(), max_rounds


# --- step serialisation ---------------------------------------------------------------------

_MAX_STR = 4000


def _clip(s: Any, n: int = _MAX_STR) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[:n] + f"… [{len(s) - n} more chars]"
    return s


def _msg_dict(m: BaseMessage) -> dict:
    d: dict[str, Any] = {"type": m.type, "content": _clip(m.content if isinstance(m.content, str)
                                                       else json.dumps(m.content, default=str))}
    if isinstance(m, AIMessage):
        if m.tool_calls:
            d["tool_calls"] = [{"name": t["name"], "args": t.get("args"), "id": t.get("id")} for t in m.tool_calls]
        if m.usage_metadata:
            d["usage"] = usage_of(m)
    if isinstance(m, ToolMessage):
        d["name"], d["tool_call_id"] = m.name, m.tool_call_id
    return d


def _jsonable(update: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in (update or {}).items():
        if k == "messages":
            out[k] = [_msg_dict(m) for m in v]
        else:
            out[k] = json.loads(json.dumps(v, default=str))
    return out


async def review_pull_request(
    *,
    repo: str,
    pr_number: int,
    client: ReadOnlyGitHubClient,
    llm: BaseChatModel,
    callbacks: list | None = None,
    metadata: dict | None = None,
    on_step: Callable[[StepEvent], Awaitable[None]] | None = None,
    max_tool_rounds: int | None = None,
) -> ReviewOutcome:
    graph, max_rounds = build_graph(client=client, llm=llm, max_tool_rounds=max_tool_rounds)
    config: RunnableConfig = {
        "callbacks": callbacks or [],
        "metadata": metadata or {},
        "run_name": "pr-review",
        "recursion_limit": 2 * max_rounds + 10,
    }
    t_start = time.perf_counter()
    t_prev = t_start
    usage = dict(_EMPTY_USAGE)
    result: dict | None = None
    dropped: list[dict] = []
    parse_failures = synthesis_attempts = 0
    rounds = 0
    pending_tool_calls: list[dict] = []

    async for chunk in graph.astream({"repo": repo, "pr_number": pr_number}, config, stream_mode="updates"):
        for node, update in chunk.items():
            if node.startswith("__"):
                continue
            now = time.perf_counter()
            latency_ms = int((now - t_prev) * 1000)
            t_prev = now
            update = update or {}
            if "usage" in update:
                usage = _merge_usage(usage, update["usage"])
            step_input: dict | None = None
            if node == "fetch_context":
                step_input = {"repo": repo, "pr_number": pr_number}
            elif node == "analyze":
                step_input = {"round": rounds}
                msgs = update.get("messages") or []
                pending_tool_calls = (
                    [{"name": t["name"], "args": t.get("args")} for t in msgs[-1].tool_calls]
                    if msgs and isinstance(msgs[-1], AIMessage) else []
                )
            elif node == "tools":
                rounds += 1
                step_input = {"tool_calls": pending_tool_calls}
            elif node == "synthesize":
                result = update.get("result")
                dropped = update.get("dropped_findings") or []
                parse_failures = int(update.get("parse_failures") or 0)
                synthesis_attempts = int(update.get("synthesis_attempts") or 0)
            if on_step is not None:
                await on_step(StepEvent(name=node, input=step_input, output=_jsonable(update), latency_ms=latency_ms))

    if result is None:
        raise SynthesisError("graph finished without a synthesize result")
    return ReviewOutcome(
        result=ReviewResult.model_validate(result),
        usage=usage,
        calls=list(client.calls),
        duration_s=time.perf_counter() - t_start,
        dropped_findings=dropped,
        parse_failures=parse_failures,
        synthesis_attempts=synthesis_attempts,
    )
