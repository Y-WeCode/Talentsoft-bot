"""File de jobs asynchrones (Redis). Désactivée par défaut (TS_ASYNC_JOBS_ENABLED)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from . import config

logger = logging.getLogger(__name__)

QUEUE_PUSH = "ts:jobs:push"
QUEUE_READ = "ts:jobs:read"
JOB_KEY_PREFIX = "ts:job:"
IDEM_JOB_KEY_PREFIX = "ts:idemjob:"
JOB_TTL_SECONDS = 86400


def is_async_jobs_enabled() -> bool:
    return config.async_jobs_enabled()


def _redis():
    url = config.redis_url()
    if not url:
        return None
    import redis

    return redis.Redis.from_url(url, decode_responses=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _enqueue(job_type: str, payload: dict[str, Any], queue: str, idempotency_key: str | None) -> dict[str, Any]:
    if not is_async_jobs_enabled():
        raise RuntimeError("Async jobs not enabled")
    r = _redis()
    if r is None:
        raise RuntimeError("REDIS_URL missing")

    if idempotency_key:
        existing = r.get(f"{IDEM_JOB_KEY_PREFIX}{idempotency_key}")
        if existing:
            job = get_job(existing)
            if job:
                return job

    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "type": job_type,
        "status": "queued",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "payload": payload,
        "result": None,
        "error": None,
        "mutation_started": False,
        "idempotency_key": idempotency_key,
    }
    pipe = r.pipeline()
    pipe.set(f"{JOB_KEY_PREFIX}{job_id}", json.dumps(job), ex=JOB_TTL_SECONDS)
    if idempotency_key:
        pipe.set(f"{IDEM_JOB_KEY_PREFIX}{idempotency_key}", job_id, ex=JOB_TTL_SECONDS, nx=True)
    pipe.lpush(queue, job_id)
    pipe.execute()
    logger.info(f"enqueued job_id={job_id} type={job_type}")
    return job


def enqueue_update_application(
    *,
    candidate_email: str,
    offer_id: str,
    event_type: str | None,
    comment: str | None,
    event_date: str | None,
    document_paths: list[str],
    document_category: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    payload = {
        "candidate_email": candidate_email,
        "offer_id": offer_id,
        "event_type": event_type,
        "comment": comment,
        "event_date": event_date,
        "document_paths": document_paths,
        "document_category": document_category,
    }
    return _enqueue("update-application", payload, QUEUE_PUSH, idempotency_key)


def get_job(job_id: str) -> dict[str, Any] | None:
    r = _redis()
    if r is None:
        return None
    raw = r.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        return None
    return json.loads(raw)


def save_job(job: dict[str, Any]) -> None:
    r = _redis()
    if r is None:
        return
    job["updated_at"] = _now_iso()
    r.set(f"{JOB_KEY_PREFIX}{job['id']}", json.dumps(job), ex=JOB_TTL_SECONDS)


def brpop_next_job(timeout_seconds: int = 5) -> tuple[str, str] | None:
    r = _redis()
    if r is None:
        return None
    item = r.brpop([QUEUE_READ, QUEUE_PUSH], timeout=timeout_seconds)
    if not item:
        return None
    queue_name, job_id = item
    return queue_name, job_id
