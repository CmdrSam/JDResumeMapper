from __future__ import annotations

import os
from typing import Any

from redis import Redis
from rq import Queue

QUEUE_NAME = "match_jobs"
JOBMETA_PREFIX = "jobmeta:"


def get_redis_connection() -> Redis:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return Redis.from_url(url)


def get_match_queue() -> Queue:
    conn = get_redis_connection()
    return Queue(QUEUE_NAME, connection=conn)


def set_job_meta(job_id: str, data: dict[str, Any], ttl_seconds: int = 7 * 24 * 3600) -> None:
    conn = get_redis_connection()
    key = f"{JOBMETA_PREFIX}{job_id}"
    payload = {k: str(v) for k, v in data.items()}
    if payload:
        conn.hset(key, mapping=payload)
    conn.expire(key, ttl_seconds)


def get_job_meta(job_id: str) -> dict[str, str]:
    conn = get_redis_connection()
    key = f"{JOBMETA_PREFIX}{job_id}"
    raw = conn.hgetall(key)
    out: dict[str, str] = {}
    for k, v in raw.items():
        out[k.decode("utf-8")] = v.decode("utf-8")
    return out

