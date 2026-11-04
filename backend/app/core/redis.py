from functools import lru_cache

import redis

from app.core.config import settings


@lru_cache
def get_redis_client() -> redis.Redis:
    """One shared Redis client (and connection pool) per process."""
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
