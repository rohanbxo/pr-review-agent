from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """FastAPI dependency. Tests override it with fakeredis."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis
