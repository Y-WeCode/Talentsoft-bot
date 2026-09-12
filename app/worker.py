"""Worker Redis : exécute les jobs navigateur hors du processus API.

Un job dont mutation_started est déjà vrai n'est jamais rejoué (anti doublon).
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ts-worker")

from app import idempotency, jobs, safety  # noqa: E402
from app.main import run_with_session  # noqa: E402
from app.scraper import sweep_old_traces  # noqa: E402
from app.session_manager import session_manager  # noqa: E402


def _cleanup_documents(paths: list[str]) -> None:
    for path in paths or []:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _process_job(job: dict) -> None:
    job_id = job["id"]
    job_type = job.get("type")
    payload = job.get("payload") or {}

    if job.get("mutation_started"):
        logger.warning(f"job_id={job_id} mutation_started already: pas de rejeu")
        job["status"] = "failed"
        job["error"] = "mutation_started_no_rejeu"
        jobs.save_job(job)
        _cleanup_documents(payload.get("document_paths") or [])
        return

    job["status"] = "running"
    job["mutation_started"] = True
    jobs.save_job(job)

    try:
        if job_type == "update-application":

            def work(bot):
                return bot.update_application(
                    application_id=payload["application_id"],
                    application_url=payload["application_url"],
                    event_type=payload.get("event_type"),
                    comment=payload.get("comment"),
                    event_date=payload.get("event_date"),
                    document_paths=payload.get("document_paths") or [],
                    document_category=payload.get("document_category"),
                )

            update_info = run_with_session(work, lock_timeout_seconds=1800)
            success = safety.actions_succeeded((update_info or {}).get("actions") or {})
            job["result"] = {"success": success, "update_details": update_info}
            job["status"] = "completed"
            if job.get("idempotency_key"):
                idempotency.store_result(job["idempotency_key"], job["result"])
        else:
            job["status"] = "failed"
            job["error"] = f"unknown_type:{job_type}"

        jobs.save_job(job)
        logger.info(f"job_id={job_id} status={job['status']}")
    except Exception as error:
        logger.exception(f"job_id={job_id} failed: {type(error).__name__}")
        job["status"] = "failed"
        job["error"] = type(error).__name__
        jobs.save_job(job)
        if job.get("idempotency_key"):
            idempotency.release(job["idempotency_key"])
    finally:
        _cleanup_documents(payload.get("document_paths") or [])


def main() -> int:
    if not jobs.is_async_jobs_enabled():
        logger.error("TS_ASYNC_JOBS_ENABLED/REDIS_URL non configurés : arrêt du worker")
        return 1

    sweep_old_traces()
    logger.info("Worker démarré")
    while True:
        item = jobs.brpop_next_job(timeout_seconds=5)
        if not item:
            continue
        _queue_name, job_id = item
        job = jobs.get_job(job_id)
        if not job:
            logger.warning(f"missing job_id={job_id}")
            continue
        _process_job(job)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        session_manager.shutdown()
