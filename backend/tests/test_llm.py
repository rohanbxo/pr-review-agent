"""Provider abstraction: one construction point, both providers selectable, and the real
ChatOpenAI client driven end to end through the review graph against a stub server (no key,
no network)."""

import json

import pytest

from app.agent import llm as llm_mod
from app.agent.fixtures import mock_transport_for_case
from app.agent.github_client import ReadOnlyGitHubClient
from app.agent.graph import SynthesisError, review_pull_request
from app.agent.llm import LLMConfigError, build_llm, configured_model_label, resolve_llm_config
from app.config import get_settings
from tests.helpers.openai_stub import VALID_REVIEW, OpenAIStub


@pytest.fixture
def settings_env(monkeypatch):
    """Set LLM env vars and clear the settings cache around each test."""
    for var in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "AGENT_MODEL", "LLM_MODEL", "ANTHROPIC_API_KEY",
                "LLM_TEMPERATURE", "LLM_PROMPT_CACHE", "LLM_KEEP_TOOL_RESULTS", "LLM_PROVIDER_ROUTING"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(__import__("tempfile").gettempdir())  # no stray .env

    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


def test_default_provider_is_openrouter(settings_env):
    settings_env()
    s = get_settings()
    assert s.llm_provider == "openai_compatible"
    assert s.llm_base_url == "https://openrouter.ai/api/v1"


def test_openai_compatible_builds_chat_openai_with_base_url_and_key(settings_env):
    from langchain_openai import ChatOpenAI

    settings_env(LLM_API_KEY="sk-or-test", AGENT_MODEL="anthropic/claude-test")
    cfg = resolve_llm_config()
    assert (cfg.provider, cfg.model, cfg.base_url) == ("openai_compatible", "anthropic/claude-test",
                                                       "https://openrouter.ai/api/v1")
    model = build_llm(cfg)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "anthropic/claude-test"
    assert model.openai_api_base == "https://openrouter.ai/api/v1"
    assert model.openai_api_key.get_secret_value() == "sk-or-test"


def test_anthropic_stays_selectable(settings_env):
    from langchain_anthropic import ChatAnthropic

    settings_env(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test")
    cfg = resolve_llm_config()
    assert (cfg.provider, cfg.model, cfg.base_url) == ("anthropic", "claude-sonnet-5", None)
    model = build_llm(cfg)
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-sonnet-5"
    # LLM_API_KEY wins over ANTHROPIC_API_KEY when both are set
    settings_env(LLM_API_KEY="sk-generic")
    assert resolve_llm_config().api_key == "sk-generic"


def test_call_site_overrides_beat_settings(settings_env):
    settings_env(LLM_API_KEY="k", AGENT_MODEL="anthropic/sonnet")
    assert resolve_llm_config(model="anthropic/haiku").model == "anthropic/haiku"
    cfg = resolve_llm_config(provider="anthropic", model="claude-haiku-4-5-20251001")
    assert (cfg.provider, cfg.base_url) == ("anthropic", None)


def test_llm_model_is_accepted_as_an_alias_for_agent_model(settings_env):
    settings_env(LLM_API_KEY="k", LLM_MODEL="anthropic/old-name")
    assert resolve_llm_config().model == "anthropic/old-name"
    settings_env(AGENT_MODEL="anthropic/new-name")
    assert resolve_llm_config().model == "anthropic/new-name"


@pytest.mark.parametrize("env,match", [
    ({"LLM_API_KEY": "k"}, "AGENT_MODEL"),                                  # openai_compatible has no default model
    ({"AGENT_MODEL": "anthropic/x"}, "LLM_API_KEY"),                         # no key
    ({"LLM_PROVIDER": "anthropic"}, "LLM_API_KEY"),
    ({"AGENT_MODEL": "m", "LLM_API_KEY": "k", "LLM_BASE_URL": ""}, "LLM_BASE_URL"),
])
def test_misconfiguration_fails_before_any_request(settings_env, env, match):
    settings_env(**env)
    with pytest.raises(LLMConfigError, match=match):
        resolve_llm_config()


def test_unknown_provider_override_is_rejected(settings_env):
    settings_env(LLM_API_KEY="k", AGENT_MODEL="m")
    with pytest.raises(LLMConfigError, match="unknown LLM provider"):
        resolve_llm_config(provider="bedrock")


def test_configured_model_label_never_raises(settings_env):
    settings_env()
    assert configured_model_label() == "openai_compatible:unconfigured"
    settings_env(AGENT_MODEL="anthropic/x")
    assert configured_model_label() == "anthropic/x"  # no key needed just to label a queued run


def test_describe_never_leaks_the_key(settings_env):
    settings_env(LLM_API_KEY="sk-secret", AGENT_MODEL="m")
    assert "sk-secret" not in json.dumps(resolve_llm_config().describe())


def test_runner_and_eval_share_the_single_construction_point():
    import inspect

    from app.agent import runner

    assert runner.get_llm is llm_mod.get_llm
    eval_src = open(__import__("pathlib").Path(__file__).parents[2] / "eval" / "run_eval.py", encoding="utf-8").read()
    assert "from app.agent.llm import build_llm" in eval_src
    assert "ChatOpenAI(" not in eval_src and "ChatAnthropic(" not in eval_src
    assert "ChatOpenAI(" not in inspect.getsource(runner)


# --- the real ChatOpenAI through the real graph, against a stub server --------------------

CASE = {
    "id": "stub-1", "split": "clean", "repo": "octo/demo", "pr_number": 7, "title": "Add helper", "body": "",
    "base_sha": "a" * 40, "head_sha": "b" * 40, "expected": None,
    "files": [{"filename": "src/a.py", "status": "modified", "additions": 1, "deletions": 0,
               "patch": "@@ -1,2 +1,3 @@\n x = 1\n+y = 2\n z = 3", "content": "x = 1\ny = 2\nz = 3\n"}],
}


async def _review_with_stub(settings_env, monkeypatch, stub: OpenAIStub):
    settings_env(LLM_API_KEY="sk-or-test", AGENT_MODEL="anthropic/claude-test")
    monkeypatch.setattr(llm_mod, "default_http_async_client", stub.async_client)
    model = build_llm(resolve_llm_config())
    client = ReadOnlyGitHubClient(token=None, transport=mock_transport_for_case(CASE))
    try:
        return await review_pull_request(repo=CASE["repo"], pr_number=CASE["pr_number"], client=client, llm=model)
    finally:
        await client.aclose()


async def test_chat_openai_end_to_end_through_graph(settings_env, monkeypatch):
    review = {**VALID_REVIEW, "files_reviewed": ["src/a.py"],
              "findings": [{"file": "src/a.py", "lines": {"start": 2, "end": 2}, "severity": "medium",
                            "title": "t", "detail": "d", "suggestion": None}]}
    stub = OpenAIStub(synthesis_payloads=[json.dumps(review)])
    outcome = await _review_with_stub(settings_env, monkeypatch, stub)

    assert outcome.parse_failures == 0 and outcome.synthesis_attempts == 1
    assert [f.file for f in outcome.result.findings] == ["src/a.py"]
    # analyze (tool call) + analyze (answer) + synthesize = 3 completions, usage summed from the responses
    assert len(stub.requests) == 3
    assert outcome.usage == {"input_tokens": 300, "output_tokens": 60, "total_tokens": 360,
                             "cache_read_input_tokens": 180, "cache_creation_input_tokens": 30, "cost_usd": 0.003}
    first, synth = stub.requests[0], stub.requests[-1]
    assert first["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert first["authorization"] == "Bearer sk-or-test"
    assert first["body"]["model"] == "anthropic/claude-test"
    assert {t["function"]["name"] for t in first["body"]["tools"]} == {
        "get_pull_request", "list_changed_files", "read_file", "list_review_comments"}
    # synthesize: forced function call (not response_format json_schema), and no review tools bound
    assert [t["function"]["name"] for t in synth["body"]["tools"]] == ["ReviewResult"]
    assert synth["body"]["tool_choice"]["function"]["name"] == "ReviewResult"
    assert "response_format" not in synth["body"]


async def test_parse_failure_repaired_on_retry_is_counted(settings_env, monkeypatch):
    stub = OpenAIStub(synthesis_payloads=['{"summary": "missing fields"', json.dumps(VALID_REVIEW)])
    outcome = await _review_with_stub(settings_env, monkeypatch, stub)
    assert (outcome.parse_failures, outcome.synthesis_attempts) == (1, 2)
    assert stub.synthesis_calls == 2


async def test_unrecoverable_parse_failure_raises_with_count(settings_env, monkeypatch):
    stub = OpenAIStub(synthesis_payloads=['{"summary": 3}'])
    with pytest.raises(SynthesisError) as info:
        await _review_with_stub(settings_env, monkeypatch, stub)
    assert (info.value.parse_failures, info.value.attempts) == (2, 2)


# --- prompt caching with context trimming -------------------------------------------------------

def _is_synth(body: dict) -> bool:
    return [t["function"]["name"] for t in body.get("tools") or []] == ["ReviewResult"]


async def test_trimming_off_by_default_uses_rolling_top_level_cache(settings_env, monkeypatch):
    stub = OpenAIStub()
    await _review_with_stub(settings_env, monkeypatch, stub)
    analyze = [r["body"] for r in stub.requests if not _is_synth(r["body"])]
    synth = [r["body"] for r in stub.requests if _is_synth(r["body"])]
    assert analyze and synth
    for body in analyze:
        assert body["cache_control"] == {"type": "ephemeral"}      # rolls over the whole conversation
        assert "cache_control" not in json.dumps(body["messages"])  # never mixed with explicit breakpoints
    for body in synth:  # different tool list: the cache can't be read, so no marker at all
        assert "cache_control" not in json.dumps(body)


async def test_trimming_on_uses_explicit_breakpoints_and_never_top_level(settings_env, monkeypatch):
    monkeypatch.setenv("LLM_KEEP_TOOL_RESULTS", "2")
    stub = OpenAIStub()
    await _review_with_stub(settings_env, monkeypatch, stub)
    analyze = [r["body"] for r in stub.requests if not _is_synth(r["body"])]
    synth = [r["body"] for r in stub.requests if _is_synth(r["body"])]
    for body in analyze:
        assert "cache_control" not in body  # top-level + explicit breakpoints don't mix (probe)
        brief = next(m for m in body["messages"] if m["role"] == "user")
        assert brief["content"][0]["cache_control"] == {"type": "ephemeral"}
    for body in synth:
        assert "cache_control" not in json.dumps(body)


async def test_provider_routing_is_sent_and_merged_with_cache_control(settings_env, monkeypatch):
    routing = {"only": ["novita"], "allow_fallbacks": False}
    monkeypatch.setenv("LLM_PROVIDER_ROUTING", json.dumps(routing))
    stub = OpenAIStub()
    await _review_with_stub(settings_env, monkeypatch, stub)
    assert resolve_llm_config().describe()["routing"] == routing
    for r in stub.requests:
        assert r["body"]["provider"] == routing  # every call, analyze AND synthesize
    assert any("cache_control" in r["body"] for r in stub.requests if not _is_synth(r["body"]))


async def test_caching_never_changes_what_the_model_sees(settings_env, monkeypatch):
    """LLM_PROMPT_CACHE on vs off: identical text and tools; only breakpoint markers differ."""
    def texts(stub):
        out = []
        for r in stub.requests:
            b = r["body"]
            msgs = []
            for m in b["messages"]:
                c = m.get("content")
                msgs.append((m["role"], c if isinstance(c, str) or c is None else "".join(x["text"] for x in c)))
            out.append((msgs, b.get("tools"), b.get("tool_choice")))
        return out

    on = OpenAIStub()
    await _review_with_stub(settings_env, monkeypatch, on)
    off = OpenAIStub()
    settings_env(LLM_PROMPT_CACHE="false")
    await _review_with_stub(settings_env, monkeypatch, off)
    assert "cache_control" not in json.dumps([r["body"] for r in off.requests])
    assert texts(on) == texts(off)


def test_cache_aware_chat_openai_keeps_breakpoint_on_tool_message(settings_env):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    settings_env(LLM_API_KEY="k", AGENT_MODEL="m")
    model = build_llm(resolve_llm_config())
    msgs = [SystemMessage("sys"), HumanMessage("brief"),
            AIMessage("", tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1"}]),
            ToolMessage([{"type": "text", "text": "stub", "cache_control": {"type": "ephemeral"}}], tool_call_id="c1")]
    wire = model._get_request_payload(msgs)["messages"]
    assert wire[3]["role"] == "tool"
    assert wire[3]["content"] == [{"type": "text", "text": "stub", "cache_control": {"type": "ephemeral"}}]
    assert wire[1]["content"] == "brief"  # unmarked messages untouched


def test_chat_anthropic_keeps_breakpoint_on_tool_result():
    from langchain_anthropic.chat_models import _format_messages
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    msgs = [HumanMessage("brief"), AIMessage("", tool_calls=[{"name": "read_file", "args": {}, "id": "c1"}]),
            ToolMessage([{"type": "text", "text": "stub", "cache_control": {"type": "ephemeral"}}], tool_call_id="c1")]
    _system, formatted = _format_messages(msgs)
    assert "cache_control" in json.dumps(formatted[-1])


# --- temperature ---------------------------------------------------------------------------------

def test_temperature_defaults_to_zero_on_both_providers(settings_env):
    settings_env(LLM_API_KEY="k", AGENT_MODEL="anthropic/x")
    cfg = resolve_llm_config()
    assert cfg.temperature == 0.0 and cfg.describe()["temperature"] == 0.0
    assert build_llm(cfg).temperature == 0.0
    settings_env(LLM_PROVIDER="anthropic")
    assert build_llm(resolve_llm_config()).temperature == 0.0


async def test_temperature_zero_is_sent_on_every_call(settings_env, monkeypatch):
    stub = OpenAIStub()
    await _review_with_stub(settings_env, monkeypatch, stub)
    assert {r["body"].get("temperature") for r in stub.requests} == {0.0}


def test_temperature_override_and_validation(settings_env):
    settings_env(LLM_API_KEY="k", AGENT_MODEL="m", LLM_TEMPERATURE="0.7")
    assert resolve_llm_config().temperature == 0.7
    assert resolve_llm_config(temperature=0.0).temperature == 0.0
    with pytest.raises(LLMConfigError, match="temperature"):
        resolve_llm_config(temperature=1.5)
