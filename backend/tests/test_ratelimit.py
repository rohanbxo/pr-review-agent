"""App-layer rate limits: sliding-window log in Redis (fakeredis), keyed on user id."""

import types

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import ratelimit
from app.config import get_settings
from app.models import AuditLog, Outcome, RepoGrant, ReviewRun, Role
from app.ratelimit import SlidingWindowLimiter, enforce_general_limit


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(ratelimit, "_now", c)
    return c


@pytest.fixture
def review_env(monkeypatch):
    """Grant-passing, GitHub-permitting environment with the agent run stubbed out."""
    from app.routers import reviews

    queued: list = []
    github_allows = {"value": True}

    github_checks: list = []

    async def fake_can_read_repo(*, user_login, repo_full_name, transport=None):
        github_checks.append(repo_full_name)
        return github_allows["value"]

    async def fake_run_review(run_id):
        queued.append(run_id)

    monkeypatch.setattr(reviews, "can_read_repo", fake_can_read_repo)
    monkeypatch.setattr(reviews, "run_review", fake_run_review)
    return types.SimpleNamespace(queued=queued, github_allows=github_allows, github_checks=github_checks)


async def _grant(session, user, repo="acme/widgets"):
    session.add(RepoGrant(user_id=user.id, repo_full_name=repo))
    await session.commit()


async def _post(client, headers, repo="acme/widgets", pr=1):
    return await client.post("/reviews", json={"repo": repo, "pr_number": pr}, headers=headers)


# --- limiter unit tests -----------------------------------------------------------------


async def test_sliding_window_does_not_double_budget_at_boundary(redis):
    clock = FakeClock(1_000_000.0)  # 1_000_000 % 60 == 40
    limiter = SlidingWindowLimiter(redis, clock=clock)
    clock.t = 1_000_019.0  # 59s into a fixed minute bucket starting at 999_960
    for _ in range(5):
        assert (await limiter.hit("k", 5, 60)).allowed

    # A fixed window would reset at 1_000_020. Two seconds later we must still be denied.
    clock.t = 1_000_021.0
    res = await limiter.hit("k", 5, 60)
    assert not res.allowed
    assert res.remaining == 0
    assert res.retry_after == 58  # oldest (…019) + 60 - now (…021)

    # Still denied a hair before the oldest entry leaves the window.
    clock.t = 1_000_078.5
    res = await limiter.hit("k", 5, 60)
    assert not res.allowed and res.retry_after == 1

    # Once the window has slid past the burst, the full budget is back — exactly once.
    clock.t = 1_000_079.01
    results = [await limiter.hit("k", 5, 60) for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]

    # Over any 60s span, never more than 5 allowed.
    assert await redis.zcard("k") == 5


async def test_retry_after_tracks_oldest_entry_in_window(redis):
    clock = FakeClock(5_000.0)
    limiter = SlidingWindowLimiter(redis, clock=clock)
    for step in (0, 10, 20):  # hits at 5000, 5010, 5020
        clock.t = 5_000.0 + step
        assert (await limiter.hit("k", 3, 100)).allowed

    clock.t = 5_030.0
    res = await limiter.hit("k", 3, 100)
    assert (res.allowed, res.retry_after) == (False, 70)  # 5000 + 100 - 5030

    clock.t = 5_100.5  # first hit expired; the next oldest (5010) governs after this one
    res = await limiter.hit("k", 3, 100)
    assert res.allowed and res.remaining == 0
    clock.t = 5_101.0
    res = await limiter.hit("k", 3, 100)
    assert (res.allowed, res.retry_after) == (False, 9)  # 5010 + 100 - 5101


async def test_denied_hits_do_not_extend_the_lockout(redis):
    clock = FakeClock(0.0 + 10_000)
    limiter = SlidingWindowLimiter(redis, clock=clock)
    assert (await limiter.hit("k", 1, 60)).allowed
    for _ in range(50):
        clock.advance(1)
        assert not (await limiter.hit("k", 1, 60)).allowed
    assert await redis.zcard("k") == 1
    clock.t = 10_060.001
    assert (await limiter.hit("k", 1, 60)).allowed


async def test_remaining_counts_down_and_key_expires(redis):
    limiter = SlidingWindowLimiter(redis, clock=FakeClock(100.0))
    assert [(await limiter.hit("k", 3, 60)).remaining for _ in range(3)] == [2, 1, 0]
    ttl = await redis.ttl("k")
    assert 0 < ttl <= 61


async def test_limits_are_keyed_per_user(redis, clock):
    limit = get_settings().rate_limit_general_per_minute
    for _ in range(limit):
        await enforce_general_limit(1, redis)
    with pytest.raises(HTTPException):
        await enforce_general_limit(1, redis)
    await enforce_general_limit(2, redis)  # another user is unaffected
    assert await redis.zcard("rl:general:1") == limit
    assert await redis.zcard("rl:general:2") == 1


async def test_general_limit_600_per_minute(redis, clock):
    assert get_settings().rate_limit_general_per_minute == 600
    for _ in range(600):
        await enforce_general_limit(42, redis)
    clock.advance(15)
    with pytest.raises(HTTPException) as exc:
        await enforce_general_limit(42, redis)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "45"
    clock.advance(45.001)
    await enforce_general_limit(42, redis)


# --- through the HTTP API ---------------------------------------------------------------


async def test_general_limit_applies_to_authenticated_requests_per_user_not_ip(
    app, client, make_user, auth_headers, clock, monkeypatch
):
    monkeypatch.setattr(get_settings(), "rate_limit_general_per_minute", 3)
    alice = await make_user(Role.reviewer)
    bob = await make_user(Role.reviewer)

    for _ in range(3):
        assert (await client.get("/reviews", headers=auth_headers(alice))).status_code == 200
    r = await client.get("/reviews", headers=auth_headers(alice))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == 60

    # Same IP, different user: fine.
    assert (await client.get("/reviews", headers=auth_headers(bob))).status_code == 200

    # Same user, different IP: still limited.
    other_ip = httpx.ASGITransport(app=app, client=("198.51.100.9", 40000))
    async with httpx.AsyncClient(transport=other_ip, base_url="http://test") as c2:
        r = await c2.get("/reviews", headers=auth_headers(alice))
    assert r.status_code == 429


async def test_21st_review_in_an_hour_is_429_with_retry_after_and_audited(
    client, session, make_user, auth_headers, clock, review_env
):
    user = await make_user(Role.reviewer)
    await _grant(session, user)
    headers = auth_headers(user)

    for i in range(20):
        r = await _post(client, headers, pr=i + 1)
        assert r.status_code == 202, r.text
        clock.advance(1)

    clock.advance(100)  # first review was at t0; now t0 + 120
    r = await _post(client, headers, pr=99)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(3600 - 120)
    assert len(review_env.queued) == 20

    runs = (await session.scalars(select(ReviewRun))).all()
    assert len(runs) == 20

    rows = (
        await session.scalars(select(AuditLog).where(AuditLog.action == "review.quota_exceeded"))
    ).all()
    assert len(rows) == 1
    assert rows[0].outcome == Outcome.denied
    assert rows[0].actor_user_id == user.id
    assert rows[0].actor_ip == "203.0.113.7"
    assert rows[0].metadata_["limit"] == 20

    # An hour after the first review, one slot frees up.
    clock.advance(3600 - 120 + 0.01)
    assert (await _post(client, headers, pr=100)).status_code == 202


async def test_over_quota_request_does_not_spend_github_api_budget(
    client, session, make_user, auth_headers, clock, review_env
):
    """Found running the real stack: the 21st review still called GitHub before hitting the
    quota, so an over-quota user could drain the App's API budget. The quota is peeked first."""
    user = await make_user(Role.reviewer)
    await _grant(session, user)
    headers = auth_headers(user)
    for i in range(20):
        assert (await _post(client, headers, pr=i + 1)).status_code == 202
    assert len(review_env.github_checks) == 20

    for _ in range(3):
        r = await _post(client, headers, pr=99)
        assert r.status_code == 429 and "Retry-After" in r.headers
    assert len(review_env.github_checks) == 20  # GitHub never consulted once over quota


async def test_admin_quota_is_100_per_hour(
    client, session, make_user, auth_headers, clock, review_env
):
    admin = await make_user(Role.admin)
    await _grant(session, admin, "acme/*")
    headers = auth_headers(admin)
    for i in range(100):
        assert (await _post(client, headers, pr=i + 1)).status_code == 202
    r = await _post(client, headers, pr=101)
    assert r.status_code == 429
    assert "Retry-After" in r.headers


async def test_viewer_has_no_review_quota_and_does_not_consume_one(
    client, redis, make_user, auth_headers, clock, review_env
):
    viewer = await make_user(Role.viewer)
    assert ratelimit.review_quota_for(Role.viewer) == 0
    assert (await _post(client, auth_headers(viewer))).status_code == 403
    assert await redis.exists(f"rl:review:{viewer.id}") == 0


async def test_denied_and_forbidden_requests_do_not_consume_quota(
    client, session, redis, make_user, auth_headers, clock, review_env
):
    user = await make_user(Role.reviewer)
    headers = auth_headers(user)

    # No grant: 403, repeatedly.
    for _ in range(25):
        assert (await _post(client, headers)).status_code == 403
    # Invalid body: 422.
    for _ in range(5):
        assert (await _post(client, headers, repo="../etc")).status_code == 422
    assert await redis.exists(f"rl:review:{user.id}") == 0

    # Grant but GitHub says no: 403.
    await _grant(session, user)
    review_env.github_allows["value"] = False
    for _ in range(25):
        assert (await _post(client, headers)).status_code == 403
    assert await redis.exists(f"rl:review:{user.id}") == 0

    # Full quota still available; 429s after exhaustion do not push the reset back.
    review_env.github_allows["value"] = True
    for i in range(20):
        assert (await _post(client, headers, pr=i + 1)).status_code == 202
    for _ in range(10):
        clock.advance(60)
        assert (await _post(client, headers)).status_code == 429
    assert await redis.zcard(f"rl:review:{user.id}") == 20
    clock.advance(3600 - 600 + 0.01)
    assert (await _post(client, headers)).status_code == 202


async def test_quota_is_per_user(client, session, make_user, auth_headers, clock, review_env):
    a = await make_user(Role.reviewer)
    b = await make_user(Role.reviewer)
    await _grant(session, a)
    await _grant(session, b)
    for i in range(20):
        assert (await _post(client, auth_headers(a), pr=i + 1)).status_code == 202
    assert (await _post(client, auth_headers(a))).status_code == 429
    assert (await _post(client, auth_headers(b))).status_code == 202
