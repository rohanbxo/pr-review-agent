"""The LangGraph review agent and its runner, with a scripted fake chat model (no network)."""

import uuid

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from app.agent import runner as runner_mod
from app.agent.fixtures import mock_transport_for_case
from app.agent.github_client import ReadOnlyGitHubClient
from app.agent.graph import SynthesisError, review_pull_request
from app.agent.llm import message_text
from app.agent.prompts import UNTRUSTED_TAG
from app.agent.schema import ReviewResult
from app.models import AgentStep, ReviewRun, RunStatus, StepKind
from tests.helpers.fake_llm import FakeCall, ScriptedChatModel, ai, review, sequence_script, tool_call

CALC = '''\
def average(values):
    """Mean of a non-empty list."""
    total = 0
    for i in range(len(values) - 1):
        total += values[i]
    return total / len(values)
'''

CASE = {
    "id": "unit-calc-1",
    "split": "injected",
    "repo": "acme/calc",
    "pr_number": 7,
    "title": "Speed up average()",
    "body": "Refactor the loop.",
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
    "files": [{
        "filename": "src/calc.py", "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -1,6 +1,6 @@\n def average(values):\n     \"\"\"Mean of a non-empty list.\"\"\"\n     total = 0\n-    for i in range(len(values)):\n+    for i in range(len(values) - 1):\n         total += values[i]\n     return total / len(values)",
        "content": CALC,
    }],
    "review_comments": [],
    "issue_comments": [],
    "expected": {"bug_kind": "off_by_one_range_len", "file": "src/calc.py", "lines": {"start": 4, "end": 4}},
}

GOOD = {
    "summary": "Loop skips the last element.",
    "risk": "high",
    "findings": [
        {"file": "src/calc.py", "lines": {"start": 4, "end": 4}, "severity": "high",
         "title": "Off-by-one drops last value", "detail": "range(len(values) - 1) skips the last item.",
         "suggestion": "Use range(len(values))."},
        {"file": "src/unrelated.py", "lines": {"start": 1, "end": 2}, "severity": "medium",
         "title": "Not in this PR", "detail": "x", "suggestion": None},
    ],
    "files_reviewed": ["src/calc.py", "src/unrelated.py"],
}


def client_for(case, seen=None):
    return ReadOnlyGitHubClient("t", transport=mock_transport_for_case(case, seen=seen))


def happy_llm():
    return ScriptedChatModel(script=sequence_script(
        ai("Let me read the file.", [tool_call("read_file", {"path": "src/calc.py"})]),
        ai("Found an off-by-one on line 4."),
        review(GOOD),
    ))


async def test_graph_node_order_tool_roundtrip_and_filtering():
    llm = happy_llm()
    events = []

    async def on_step(ev):
        events.append(ev)

    async with client_for(CASE) as gh:
        outcome = await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm, on_step=on_step)

    assert [e.name for e in events] == ["fetch_context", "analyze", "tools", "analyze", "synthesize"]
    assert isinstance(outcome.result, ReviewResult)
    assert [f.file for f in outcome.result.findings] == ["src/calc.py"]
    assert outcome.result.files_reviewed == ["src/calc.py"]
    assert [f["file"] for f in outcome.dropped_findings] == ["src/unrelated.py"]
    # 2 analyze calls (100/20 each) + 1 synthesize (300/80)
    assert outcome.usage == {"input_tokens": 500, "output_tokens": 120, "total_tokens": 620,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0}

    # fetch_context is deterministic Python: PR + files fetched before the first LLM call.
    paths = [c.path for c in outcome.calls]
    assert paths[:2] == ["/repos/acme/calc/pulls/7", "/repos/acme/calc/pulls/7/files"]
    assert paths[2] == "/repos/acme/calc/contents/src/calc.py"
    assert not any(c.blocked for c in outcome.calls)

    # Tool round trip: the tool result reached the second analyze call, wrapped as untrusted data.
    second = llm.calls[1]
    tool_msgs = [m for m in second.messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content.startswith(f'<{UNTRUSTED_TAG} source="read_file"')
    assert "4|     for i in range(len(values) - 1):" in tool_msgs[0].content

    # analyze has the review tools bound; synthesize has none of them.
    assert set(llm.calls[0].tool_names) == {"get_pull_request", "list_changed_files", "read_file", "list_review_comments"}
    assert llm.calls[2].structured and llm.calls[2].tool_choice == "any"

    # step payloads are JSON-friendly and informative
    tools_ev = events[2]
    assert tools_ev.input == {"tool_calls": [{"name": "read_file", "args": {"path": "src/calc.py"}}]}
    assert events[-1].output["result"]["risk"] == "high"


async def test_first_llm_call_is_grounded_in_fetched_context():
    llm = ScriptedChatModel(script=sequence_script(ai("fine"), review({
        "summary": "ok", "risk": "low", "findings": [], "files_reviewed": ["src/calc.py"]})))
    async with client_for(CASE) as gh:
        outcome = await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm)
    first = llm.calls[0].messages
    assert first[0].type == "system" and "untrusted" in first[0].content.lower()
    assert "range(len(values) - 1)" in message_text(first[1])
    assert outcome.result.findings == []  # an empty review is valid


async def test_synthesize_repair_retry():
    bad = dict(GOOD, findings=[dict(GOOD["findings"][0], lines={"start": 9, "end": 3})])
    llm = ScriptedChatModel(script=sequence_script(ai("notes"), review(bad), review(GOOD)))
    async with client_for(CASE) as gh:
        outcome = await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm)
    assert len(outcome.result.findings) == 1
    assert "did not validate" in llm.calls[2].messages[-1].content


async def test_synthesize_fails_after_one_repair():
    llm = ScriptedChatModel(script=sequence_script(ai("notes"), ai("not json"), ai("still not json")))
    async with client_for(CASE) as gh:
        with pytest.raises(SynthesisError):
            await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm)
    assert len(llm.calls) == 3


async def test_tool_rounds_are_capped():
    def script(call: FakeCall):
        if call.structured:
            return review({"summary": "s", "risk": "low", "findings": [], "files_reviewed": []})
        return ai("more", [tool_call("list_changed_files")])

    llm = ScriptedChatModel(script=script)
    events = []

    async def on_step(ev):
        events.append(ev.name)

    async with client_for(CASE) as gh:
        await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm, on_step=on_step,
                                  max_tool_rounds=2)
    assert events == ["fetch_context", "analyze", "tools", "analyze", "tools", "analyze", "synthesize"]
    # the unanswered final tool call is closed before synthesize so the transcript stays valid
    synth = llm.calls[-1].messages
    last_ai = [m for m in synth if m.type == "ai"][-1]
    answered = {m.tool_call_id for m in synth if isinstance(m, ToolMessage)}
    assert {tc["id"] for tc in last_ai.tool_calls} <= answered


async def test_unknown_tool_and_bad_args_become_tool_errors():
    llm = ScriptedChatModel(script=sequence_script(
        ai("", [tool_call("merge_pull_request", {}), tool_call("read_file", {"nope": 1})]),
        ai("done"),
        review({"summary": "s", "risk": "low", "findings": [], "files_reviewed": []}),
    ))
    async with client_for(CASE) as gh:
        await review_pull_request(repo="acme/calc", pr_number=7, client=gh, llm=llm)
    tool_msgs = [m for m in llm.calls[1].messages if isinstance(m, ToolMessage)]
    assert "unknown tool" in tool_msgs[0].content
    assert tool_msgs[1].content.startswith("ERROR")


# --- runner -------------------------------------------------------------------------------

async def _make_run(sessionmaker, repo="acme/calc", pr=7) -> uuid.UUID:
    async with sessionmaker() as s:
        run = ReviewRun(repo_full_name=repo, pr_number=pr, model="fake", status=RunStatus.queued)
        s.add(run)
        await s.commit()
        return run.id


async def _load(sessionmaker, run_id):
    async with sessionmaker() as s:
        run = await s.get(ReviewRun, run_id)
        steps = (await s.execute(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq))).scalars().all()
        return run, steps


@pytest.fixture
def no_token(monkeypatch):
    async def _tok():
        return None

    monkeypatch.setattr(runner_mod, "_get_token", _tok)


async def test_runner_persists_node_steps_and_call_log(sessionmaker, no_token):
    run_id = await _make_run(sessionmaker)
    await runner_mod.run_review(run_id, llm=happy_llm(), transport=mock_transport_for_case(CASE))
    run, steps = await _load(sessionmaker, run_id)

    assert run.status == RunStatus.succeeded, run.error
    assert run.result["findings"][0]["file"] == "src/calc.py"
    assert run.usage["total_tokens"] == 620
    assert run.started_at and run.finished_at and run.error is None
    assert run.langfuse_trace_id is None  # tracing is a no-op without keys

    node_steps = [s for s in steps if s.kind == StepKind.node]
    assert [s.name for s in node_steps] == ["fetch_context", "analyze", "tools", "analyze", "synthesize"]
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert steps[-1].kind == StepKind.github_calls
    calls = steps[-1].output["calls"]
    assert [c["path"] for c in calls][:3] == [
        "/repos/acme/calc/pulls/7", "/repos/acme/calc/pulls/7/files", "/repos/acme/calc/contents/src/calc.py"]
    assert steps[-1].output["blocked"] == 0
    assert len(steps) == 6


async def test_runner_failure_still_persists_call_log(sessionmaker, no_token):
    run_id = await _make_run(sessionmaker, pr=999)  # the fixture has no PR 999 -> 404 in fetch_context
    await runner_mod.run_review(run_id, llm=happy_llm(), transport=mock_transport_for_case(CASE))
    run, steps = await _load(sessionmaker, run_id)
    assert run.status == RunStatus.failed
    assert "404" in run.error
    assert run.finished_at is not None and run.result is None
    assert [s.kind for s in steps] == [StepKind.error, StepKind.github_calls]
    assert steps[-1].output["calls"][0]["status"] == 404


async def test_runner_failure_midway_keeps_node_rows_and_blocked_calls(sessionmaker, no_token):
    def script(call: FakeCall):
        if len(llm.calls) == 1:
            return ai("", [tool_call("read_file", {"path": "../../etc/passwd"})])
        raise RuntimeError("model exploded")

    llm = ScriptedChatModel(script=script)
    run_id = await _make_run(sessionmaker)
    await runner_mod.run_review(run_id, llm=llm, transport=mock_transport_for_case(CASE))
    run, steps = await _load(sessionmaker, run_id)
    assert run.status == RunStatus.failed and "model exploded" in run.error
    assert [s.name for s in steps if s.kind == StepKind.node] == ["fetch_context", "analyze", "tools"]
    log = steps[-1]
    assert log.kind == StepKind.github_calls and log.output["blocked"] == 1
    assert any(c["blocked"] and "etc/passwd" in c["path"] for c in log.output["calls"])


async def test_runner_missing_run_is_noop(sessionmaker, no_token):
    await runner_mod.run_review(uuid.uuid4(), llm=happy_llm(), transport=mock_transport_for_case(CASE))


def test_langfuse_tracing_disabled_without_keys():
    assert runner_mod._langfuse_tracing() == (None, None)


class RecordingHandler(AsyncCallbackHandler):
    def __init__(self):
        self.root_metadata = None
        self.last_trace_id = "0123456789abcdef0123456789abcdef"

    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kw):
        if parent_run_id is None:
            self.root_metadata = metadata


async def test_runner_passes_langfuse_trace_attributes(sessionmaker, no_token, monkeypatch):
    handler = RecordingHandler()
    monkeypatch.setattr(runner_mod, "_langfuse_tracing", lambda: (None, handler))
    run_id = await _make_run(sessionmaker)
    async with sessionmaker() as s:
        run = await s.get(ReviewRun, run_id)
        run.user_id = None
    await runner_mod.run_review(run_id, llm=happy_llm(), transport=mock_transport_for_case(CASE))
    run, _ = await _load(sessionmaker, run_id)
    assert handler.root_metadata["langfuse_session_id"] == str(run_id)
    assert run.langfuse_trace_id == handler.last_trace_id


def test_installed_langfuse_handler_reads_our_metadata_keys():
    """Guards the v4 API we rely on: the handler maps these metadata keys to trace attributes."""
    from langfuse.langchain import CallbackHandler

    h = CallbackHandler()
    attrs = h._parse_langfuse_trace_attributes(
        metadata={"langfuse_user_id": "42", "langfuse_session_id": "run-1"}, tags=None)
    assert attrs["user_id"] == "42" and attrs["session_id"] == "run-1"
