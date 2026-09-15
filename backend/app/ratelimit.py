"""App-layer rate limits, keyed on USER id, sliding-window log in Redis.

Why a log and not a fixed window: a fixed window doubles the budget at the boundary (full
spend at 10:59:59 and again at 11:00:01). The purpose of the review quota is cost control, so
every accepted hit is stored as a member of a sorted set scored by its timestamp, and a hit is
allowed only if fewer than `limit` members fall inside the trailing `window_seconds`.

Why not nginx: nginx counts requests per IP per nginx instance. The scarce resources (LLM
spend, the GitHub App's API quota) are consumed per *review* per *user*, and these counters
live in Redis so every API replica shares them.

Atomicity: an optimistic WATCH/MULTI transaction (retried on WatchError) rather than a Lua
script. Both are atomic against concurrent writers; WATCH/MULTI was chosen because the test
double (fakeredis without `lupa`) does not implement EVAL, and a single code path that is
exercised by the tests beats a faster one that is not.

Denied hits are never recorded, so a client hammering a closed limit does not extend its own
lockout, and requests rejected for other reasons (403, 422) never reach the limiter at all.
"""

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import WatchError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import get_settings
from app.models import Outcome, Role, User
from app.request_context import RequestContext

_MAX_RETRIES = 50


def _now() -> float:
    """Default clock. Tests monkeypatch this (or pass `clock=`) instead of patching time.time."""
    return time.time()


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    remaining: int
    retry_after: int  # seconds; 0 when allowed


class SlidingWindowLimiter:
    def __init__(self, redis: Redis, clock: Callable[[], float] | None = None):
        self.redis = redis
        self.clock = clock or _now

    async def hit(self, key: str, limit: int, window_seconds: int) -> LimitResult:
        if limit <= 0:
            return LimitResult(allowed=False, remaining=0, retry_after=window_seconds)

        async with self.redis.pipeline(transaction=True) as pipe:
            for _ in range(_MAX_RETRIES):
                try:
                    await pipe.watch(key)
                    now = self.clock()
                    floor = now - window_seconds
                    # Entries with score <= floor have left the window. Reads only while
                    # watching: a write here would invalidate our own WATCH.
                    in_window = await pipe.zrangebyscore(
                        key, f"({floor}", "+inf", start=0, num=limit, withscores=True
                    )
                    count = len(in_window)

                    if count >= limit:
                        await pipe.unwatch()
                        oldest = float(in_window[0][1])
                        retry_after = max(1, math.ceil(oldest + window_seconds - now))
                        return LimitResult(allowed=False, remaining=0, retry_after=retry_after)

                    pipe.multi()
                    pipe.zremrangebyscore(key, "-inf", floor)
                    pipe.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex}": now})
                    pipe.expire(key, window_seconds + 1)
                    await pipe.execute()
                    return LimitResult(allowed=True, remaining=limit - count - 1, retry_after=0)
                except WatchError:
                    continue  # a concurrent hit changed the set; re-read and decide again
        # Persistent contention: fail closed, briefly.
        return LimitResult(allowed=False, remaining=0, retry_after=1)

    async def peek(self, key: str, limit: int, window_seconds: int) -> LimitResult:
        """Would a hit be allowed right now? Records nothing. Advisory only: `hit` is authoritative."""
        if limit <= 0:
            return LimitResult(allowed=False, remaining=0, retry_after=window_seconds)
        now = self.clock()
        in_window = await self.redis.zrangebyscore(
            key, f"({now - window_seconds}", "+inf", start=0, num=limit, withscores=True
        )
        if len(in_window) >= limit:
            oldest = float(in_window[0][1])
            return LimitResult(False, 0, max(1, math.ceil(oldest + window_seconds - now)))
        return LimitResult(True, limit - len(in_window), 0)


def _too_many(retry_after: int, detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail, headers={"Retry-After": str(retry_after)}
    )


def general_key(user_id: int) -> str:
    return f"rl:general:{user_id}"


def review_key(user_id: int) -> str:
    return f"rl:review:{user_id}"


def review_quota_for(role: Role) -> int:
    s = get_settings()
    return {
        Role.admin: s.review_quota_per_hour_admin,
        Role.reviewer: s.review_quota_per_hour_reviewer,
    }.get(role, 0)  # viewers cannot create reviews; no quota


async def enforce_general_limit(user_id: int, redis: Redis) -> None:
    """Raise HTTPException(429, headers={'Retry-After': ...}) when over the 600/min ceiling."""
    result = await SlidingWindowLimiter(redis).hit(
        general_key(user_id), get_settings().rate_limit_general_per_minute, 60
    )
    if not result.allowed:
        raise _too_many(result.retry_after, "rate limit exceeded")


async def enforce_review_quota(
    user: User, redis: Redis, session: AsyncSession, ctx: RequestContext | None,
    *, consume: bool = True,
) -> None:
    """Consume one review from the user's hourly quota, or audit the exhaustion and raise 429.

    Call it with consume=True LAST, after every check that can deny the request, so denied
    requests do not consume quota. Call it with consume=False FIRST, before anything that
    spends the GitHub App's API budget (can_read_repo), so an over-quota user cannot burn it."""
    limit = review_quota_for(user.role)
    limiter = SlidingWindowLimiter(redis)
    if consume:
        result = await limiter.hit(review_key(user.id), limit, 3600)
    else:
        result = await limiter.peek(review_key(user.id), limit, 3600)
    if result.allowed:
        return
    record_audit(
        session, ctx=ctx, actor=user, action="review.quota_exceeded", outcome=Outcome.denied,
        resource_type="user", resource_id=user.id,
        metadata={"limit": limit, "window_seconds": 3600, "retry_after": result.retry_after,
                  "role": user.role.value},
    )
    # A denial has no action to share a transaction with; commit it on its own.
    await session.commit()
    raise _too_many(result.retry_after, "review quota exceeded")
