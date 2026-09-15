"""Context hygiene: older tool results become stubs; the brief is never trimmed; cache breakpoints
sit only on positions that stay byte-identical in the next request."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.context import model_view, stub_text

BRIEF = "Review pull request #1.\n@@ -1 +1 @@\n-a\n+b"


def convo(rounds: int) -> list:
    msgs = [SystemMessage("system"), HumanMessage(BRIEF)]
    for r in range(rounds):
        msgs.append(AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": f"f{r}.py"}, "id": f"c{r}"}]))
        msgs.append(ToolMessage("x" * (1000 + r), tool_call_id=f"c{r}", name="read_file"))
    return msgs


def texts(msgs):
    return [m.content if isinstance(m.content, str) else "".join(b["text"] for b in m.content) for m in msgs]


def tool_texts(msgs):
    return [t for m, t in zip(msgs, texts(msgs)) if isinstance(m, ToolMessage)]


def test_keeps_last_n_and_stubs_older_with_tool_args_and_size():
    view = model_view(convo(4), keep=2, cache=False)
    tt = tool_texts(view)
    assert tt[2:] == ["x" * 1002, "x" * 1003]
    assert tt[0] == stub_text("read_file", {"path": "f0.py"}, 1000)
    assert 'read_file({"path": "f0.py"}) returned 1,000 characters' in tt[0]
    assert "call read_file again" in tt[0]
    assert [m.tool_call_id for m in view if isinstance(m, ToolMessage)] == ["c0", "c1", "c2", "c3"]


def test_brief_and_patches_are_never_trimmed_and_input_is_not_mutated():
    full = convo(5)
    before = texts(full)
    view = model_view(full, keep=0, cache=True)
    assert texts(view)[1] == BRIEF
    assert texts(full) == before
    assert all("Earlier tool result removed" in t for t in tool_texts(view))


def test_no_trimming_until_more_than_n_results():
    full = convo(2)
    assert texts(model_view(full, keep=2, cache=False)) == texts(full)


def test_breakpoints_only_on_brief_and_newest_stub():
    view = model_view(convo(5), keep=2, cache=True)
    marked = [i for i, m in enumerate(view)
              if isinstance(m.content, list) and any("cache_control" in b for b in m.content)]
    tool_idx = [i for i, m in enumerate(view) if isinstance(m, ToolMessage)]
    assert marked == [1, tool_idx[2]]  # brief and the newest stub (3rd of 5 results); never the last message


def test_prefix_up_to_newest_stub_is_stable_in_the_next_round():
    """The property the cache relies on: stubbing is monotonic, so the prefix ending at round k's
    newest stub is byte-identical in round k+1."""
    k = model_view(convo(4), keep=2, cache=False)
    k1 = model_view(convo(5), keep=2, cache=False)
    newest_stub_k = [i for i, m in enumerate(k) if isinstance(m, ToolMessage)][1]
    assert texts(k[: newest_stub_k + 1]) == texts(k1[: newest_stub_k + 1])


def test_stub_args_are_bounded_and_escaped():
    t = stub_text("read_file", {"path": "​" + "a" * 1000}, 5)
    assert "​" not in t and "(truncated)" in t and len(t) < 600
    with pytest.raises(ValueError):
        model_view(convo(1), keep=-1, cache=False)
