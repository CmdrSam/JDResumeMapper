from __future__ import annotations

import os

from redis import Redis
from rq import Queue

QUEUE_NAME = "match_jobs"


def get_redis_connection() -> Redis:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return Redis.from_url(url)


def get_match_queue() -> Queue:
    conn = get_redis_connection()
    return Queue(QUEUE_NAME, connection=conn)

