"""Background execution of one review run.

`agent_steps` duplicates the Langfuse trace on purpose (SPEC § Non-negotiables 8): Langfuse is
for debugging and has its own retention; the table is the record we keep. So:

* each LangGraph node update is persisted as a ``kind=node`` row as it streams;
* the GitHub call log is persisted as a ``kind=github_calls`` row in ``finally`` — whatever the
  outcome, including blocked attempts;
* a failure also adds a ``kind=error`` row and sets ``review_runs.error``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel

from app.agent.github_client import ReadOnlyGitHubClient
from app.agent.graph import StepEvent, review_pull_request
from app.agent.llm import get_llm
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import AgentStep, ReviewRun, RunStatus, StepKind, utcnow

log = logging.getLogger(__name__)


async def _get_token() -> str | None:
    # Imported lazily: a different module (and trust domain) owns the App credentials.
    from app.github_app import get_installation_token

    return await get_installation_token()


def _langfuse_tracing() -> tuple[Any, Any]:
    """(langfuse_client, langchain_callback_handler), or (None, None) when tracing is off.

    Langfuse Python SDK v4: ``langfuse.langchain.CallbackHandler`` resolves its client through
    ``get_client(public_key=...)``, so we construct the ``Langfuse`` client first with explicit
    credentials (it registers itself per public key). Trace-level ``user_id`` / ``session_id``
    are read by the handler from the root run's ``metadata`` keys ``langfuse_user_id`` and
    ``langfuse_session_id`` (it wraps the root chain in ``propagate_attributes``).
    """
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None, None
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        client = Langfuse(public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host)
        return client, CallbackHandler(public_key=s.langfuse_public_key)
    except Exception:  # noqa: BLE001 - tracing must never break a review
        log.exception("langfuse tracing disabled: initialisation failed")
        return None, None


async def run_review(
    run_id: uuid.UUID,
    *,
    llm: BaseChatModel | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Execute a queued review run. ``llm`` / ``transport`` are injection points for tests and eval."""
    maker = get_sessionmaker()
    async with maker() as session:
        run = await session.get(ReviewRun, run_id)
        if run is None:
            log.warning("run_review: run %s not found", run_id)
            return
        run.status = RunStatus.running
        run.started_at = utcnow()
        await session.commit()

        seq = 0
        client: ReadOnlyGitHubClient | None = None
        lf_client, lf_handler = _langfuse_tracing()

        def _next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        async def on_step(ev: StepEvent) -> None:
            session.add(AgentStep(
                run_id=run_id, seq=_next_seq(), kind=StepKind.node, name=ev.name,
                input=ev.input, output=ev.output, latency_ms=ev.latency_ms,
            ))
            await session.commit()

        try:
            token = await _get_token()
            client = ReadOnlyGitHubClient(token, transport=transport)
            model = llm if llm is not None else get_llm()
            metadata = {
                "langfuse_session_id": str(run_id),
                "langfuse_tags": ["pr-review"],
                "repo": run.repo_full_name,
                "pr_number": run.pr_number,
                "run_id": str(run_id),
            }
            if run.user_id is not None:
                metadata["langfuse_user_id"] = str(run.user_id)
            outcome = await review_pull_request(
                repo=run.repo_full_name,
                pr_number=run.pr_number,
                client=client,
                llm=model,
                callbacks=[lf_handler] if lf_handler is not None else None,
                metadata=metadata,
                on_step=on_step,
            )
            run.result = outcome.result.model_dump(mode="json")
            run.usage = {**outcome.usage, "duration_s": round(outcome.duration_s, 3),
                         "dropped_findings": len(outcome.dropped_findings),
                         "parse_failures": outcome.parse_failures}
            run.status = RunStatus.succeeded
        except Exception as exc:  # noqa: BLE001 - recorded on the run
            log.exception("review run %s failed", run_id)
            if session.in_transaction():
                await session.rollback()
            run = await session.get(ReviewRun, run_id)
            run.status = RunStatus.failed
            run.error = f"{type(exc).__name__}: {exc}"[:4000]
            partial = getattr(exc, "partial_usage", None)
            if partial:  # a failed review is billed: keep what it cost
                run.usage = {**partial, "partial": True}
            session.add(AgentStep(
                run_id=run_id, seq=_next_seq(), kind=StepKind.error, name=type(exc).__name__,
                input=None, output={"error": run.error}, latency_ms=None,
            ))
        finally:
            calls = [c.as_dict() for c in (client.calls if client is not None else [])]
            session.add(AgentStep(
                run_id=run_id, seq=_next_seq(), kind=StepKind.github_calls, name="github_calls",
                input=None,
                output={"calls": calls, "count": len(calls), "blocked": sum(1 for c in calls if c["blocked"])},
                latency_ms=sum(c["duration_ms"] for c in calls),
            ))
            if lf_handler is not None and getattr(lf_handler, "last_trace_id", None):
                run.langfuse_trace_id = lf_handler.last_trace_id
            run.finished_at = utcnow()
            await session.commit()
            if client is not None:
                await client.aclose()
            if lf_client is not None:
                try:
                    await asyncio.to_thread(lf_client.flush)
                except Exception:  # noqa: BLE001
                    log.exception("langfuse flush failed")
