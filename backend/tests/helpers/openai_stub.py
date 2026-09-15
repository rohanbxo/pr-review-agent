"""A stub OpenAI-compatible ``/chat/completions`` server on ``httpx.MockTransport``.

It lets the REAL ``ChatOpenAI`` client (as built by ``app.agent.llm.build_llm``) run end to end
with no network and no key: request serialisation, tool binding, forced-function structured
output, response parsing and usage accounting are all the real code paths.

Behaviour, decided from the request body the way a real model would see it:

* analyze call (the four review tools bound): first round asks for ``list_changed_files``;
  once a ``tool`` message is present it answers in plain text.
* synthesize call (only the ``ReviewResult`` function bound, forced): returns the next payload
  from ``synthesis_payloads`` as the function arguments -- pass invalid JSON or an incomplete
  object to exercise parse failures. The last payload repeats.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field

import httpx

VALID_REVIEW = {
    "summary": "Stub review.",
    "risk": "medium",
    "findings": [],
    "files_reviewed": [],
}


_PR_NUMBER = re.compile(r"Review pull request #(\d+)")


@dataclass
class OpenAIStub:
    synthesis_payloads: list[str] = field(default_factory=lambda: [json.dumps(VALID_REVIEW)])
    # Optional per-PR plans (keyed by the PR number in the prompt), for multi-case eval runs.
    payloads_by_pr: dict[int, list[str]] = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _synth_calls: int = 0
    _synth_calls_by_pr: dict[int, int] = field(default_factory=dict)

    @property
    def synthesis_calls(self) -> int:
        return self._synth_calls

    def _next_payload(self, body: dict) -> str:
        text = json.dumps(body.get("messages") or [])
        m = _PR_NUMBER.search(text)
        pr = int(m.group(1)) if m else None
        plan = self.payloads_by_pr.get(pr) if pr is not None else None
        if plan is None:
            plan, n = self.synthesis_payloads, self._synth_calls
        else:
            n = self._synth_calls_by_pr.get(pr, 0)
            self._synth_calls_by_pr[pr] = n + 1
        self._synth_calls += 1
        return plan[min(n, len(plan) - 1)]

    def _completion(self, model: str, message: dict, finish: str) -> dict:
        return {
            "id": f"chatcmpl-stub-{next(self._ids)}", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            # Shaped like OpenRouter's usage for an Anthropic model with a warm prompt cache.
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.001,
                      "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10}},
        }

    def _tool_call(self, name: str, arguments: str) -> dict:
        return {"id": f"call_{next(self._ids)}", "type": "function", "function": {"name": name, "arguments": arguments}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"url": str(request.url), "authorization": request.headers.get("authorization"),
                              "body": body})
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "stub: unknown path"}})
        model = body.get("model", "")
        tool_names = [t["function"]["name"] for t in body.get("tools") or []]

        if tool_names == ["ReviewResult"]:
            payload = self._next_payload(body)
            msg = {"role": "assistant", "content": None, "tool_calls": [self._tool_call("ReviewResult", payload)]}
            return httpx.Response(200, json=self._completion(model, msg, "tool_calls"))

        if not any(m.get("role") == "tool" for m in body.get("messages") or []):
            msg = {"role": "assistant", "content": None, "tool_calls": [self._tool_call("list_changed_files", "{}")]}
            return httpx.Response(200, json=self._completion(model, msg, "tool_calls"))
        msg = {"role": "assistant", "content": "I have what I need."}
        return httpx.Response(200, json=self._completion(model, msg, "stop"))

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
