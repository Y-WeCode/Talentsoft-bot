"""Worker Redis : **seul** processus autorisé à piloter un navigateur.

Le tenant Talentsoft n'admet qu'une session active par compte technique. Tant que l'API ouvrait
elle aussi un Chromium, les deux sessions se déconnectaient mutuellement et le bot finissait en
état dégradé. L'API est donc devenue un guichet : elle empile ici, et attend le résultat.

Deux conséquences directes :

- la session est **réutilisée d'un job à l'autre** (`session_manager` la garde ouverte), ce qui
  est la condition de tenue des dizaines de pushs par heure ;
- le worker publie un battement de cœur, sans quoi l'API ne pourrait pas distinguer « personne
  ne dépile » d'un job simplement long.

Un job dont `mutation_started` est déjà vrai n'est jamais rejoué (anti doublon). Ce drapeau
n'est plus posé au lancement mais à la **première écriture réelle** dans le Back Office : un job
qui échoue au login n'a rien modifié, et reste donc rejouable.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ts-worker")

from app import browser_runner, config, idempotency, jobs, safety  # noqa: E402
from app.scraper import sweep_old_traces  # noqa: E402
from app.session_manager import session_manager  # noqa: E402

HEARTBEAT_INTERVAL_SECONDS = 10

_current_job_id: str | None = None
_current_started_at: float | None = None
_current_lock = threading.Lock()


def _set_current_job(job_id: str | None) -> None:
    global _current_job_id, _current_started_at
    with _current_lock:
        _current_job_id = job_id
        _current_started_at = time.monotonic() if job_id else None


def _current_job_age_seconds() -> tuple[str | None, int]:
    with _current_lock:
        job_id, started = _current_job_id, _current_started_at
    if not job_id or started is None:
        return None, 0
    return job_id, int(time.monotonic() - started)


def _hard_budget_seconds() -> int:
    """Au-delà, le job n'est plus lent : le worker est bloqué. Marge au-dessus du budget normal."""
    return config.job_timeout_seconds() + 120


def _heartbeat_payload() -> dict:
    job_id, age = _current_job_age_seconds()
    return {
        "alive": True,
        "pid": os.getpid(),
        "current_job_id": job_id,
        # Un battement qui dit seulement « vivant » a laissé passer six jours de blocage : le
        # thread de battement tournait pendant que la boucle de jobs était suspendue.
        "current_job_seconds": age,
        **session_manager.status(),
    }


def _abort_if_stuck() -> None:
    """Sort du processus quand un job dépasse toute durée plausible.

    Les délais d'attente de Playwright sont appliqués **par le pilote Node**. Quand celui-ci
    meurt — une assertion interne suffit — plus rien ne les applique : l'appel Python reste
    suspendu sur un tuyau muet, sans exception ni timeout. Constaté en recette : six jours sur le
    même job, quinze jobs empilés derrière, et un battement qui annonçait un worker en bonne santé.

    Le processus est alors irrécupérable. On conclut le job pour que l'appelant cesse d'attendre,
    puis on sort : le superviseur relancera un worker neuf.
    """
    job_id, age = _current_job_age_seconds()
    if not job_id or age < _hard_budget_seconds():
        return
    logger.error(f"worker_stuck job_id={job_id} elapsed_s={age} : arrêt du processus")
    _conclude_stuck_job(job_id, age)
    os._exit(1)


def _conclude_stuck_job(job_id: str, age: int) -> None:
    """Marque le job avant de sortir. `mutation_started` est conservé : c'est lui qui dira à
    l'appelant s'il peut rejouer."""
    try:
        job = jobs.get_job(job_id)
        if job and job.get("status") == "running":
            _fail(job, "worker_stuck", f"navigateur sans réponse depuis {age} s, worker redémarré")
    except Exception as error:
        logger.error(f"worker_stuck_conclude_failed error={type(error).__name__}")


def _recover_orphan_jobs() -> None:
    """Conclut les jobs laissés `running` par un worker disparu.

    La clé d'idempotence n'est libérée que si aucune écriture n'avait été engagée — même règle
    que sur un échec ordinaire, pour ne pas rouvrir la porte au doublon.
    """
    for job_id in jobs.iter_running_job_ids():
        job = jobs.get_job(job_id)
        if not job or job.get("status") != "running":
            continue
        logger.warning(f"job_id={job_id} orphelin : worker disparu en cours de traitement")
        _fail(job, "worker_interrupted", "le worker a disparu pendant le traitement")
        _release_idempotency(job)


def _heartbeat_loop(stop: threading.Event) -> None:
    """Publie l'état du worker depuis un thread dédié.

    Surtout pas depuis la boucle `brpop` : pendant un push d'une minute le worker est bloqué
    dans Playwright et n'y repasse pas. Le battement expirerait en plein job et l'API conclurait
    à tort que le worker est mort.
    """
    while not stop.is_set():
        try:
            jobs.worker_heartbeat(_heartbeat_payload())
        except Exception as error:
            logger.warning(f"heartbeat_failed error={type(error).__name__}")
        # Ce thread est le seul encore vivant quand la boucle de jobs est suspendue : c'est donc
        # lui qui doit constater le blocage.
        _abort_if_stuck()
        stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def _cleanup_documents(paths: list[str]) -> None:
    for path in paths or []:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# --- Travaux ------------------------------------------------------------------------------


def _run_update_application(job: dict, payload: dict) -> dict:
    def on_mutation_started() -> None:
        """Première écriture réelle : le job devient non rejouable, et on le persiste tout de suite.

        Différer jusqu'à la fin du job perdrait l'information si le worker mourait pendant le
        clic — précisément le scénario que ce drapeau existe pour couvrir.
        """
        job["mutation_started"] = True
        jobs.save_job(job)
        logger.info(f"job_id={job['id']} mutation_started=true")

    def work(bot):
        return bot.update_application(
            candidate_email=payload["candidate_email"],
            offer_id=payload["offer_id"],
            event_type=payload.get("event_type"),
            comment=payload.get("comment"),
            event_date=payload.get("event_date"),
            document_paths=payload.get("document_paths") or [],
            document_category=payload.get("document_category"),
            on_mutation_started=on_mutation_started,
        )

    update_info = browser_runner.run_with_session(
        work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds()
    )
    actions = (update_info or {}).get("actions") or {}
    job["mutation_may_have_happened"] = safety.actions_may_have_mutated(actions)
    return {"success": safety.actions_succeeded(actions), "update_details": update_info}


def _run_list_events(payload: dict) -> dict:
    offer = payload["offer_id"]

    def work(bot):
        return {"offer_id": offer, "events": bot.list_events(payload["candidate_email"], offer)}

    return browser_runner.run_with_session(work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds())


def _run_referential(payload: dict, reader_name: str) -> dict:
    def work(bot):
        return {"values": getattr(bot, reader_name)(payload["candidate_email"], payload["offer_id"])}

    return browser_runner.run_with_session(work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds())


def _run_selftest(payload: dict) -> dict:
    def work(bot):
        return bot.selftest(payload.get("candidate_email") or None, payload.get("offer_id") or None)

    return browser_runner.run_with_session(work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds())


def _run_reset_session() -> dict:
    """Sortie d'état dégradé. Ne touche pas au navigateur : `request_invalidate` est thread-safe
    et la session sera fermée au prochain `get_bot`."""
    session_manager.reset_degraded()
    session_manager.request_invalidate("admin_reset")
    return {"ok": True, **session_manager.status()}


def _dispatch(job: dict, payload: dict) -> dict:
    job_type = job.get("type")
    if job_type == jobs.JOB_TYPE_UPDATE_APPLICATION:
        return _run_update_application(job, payload)
    if job_type == jobs.JOB_TYPE_LIST_EVENTS:
        return _run_list_events(payload)
    if job_type == jobs.JOB_TYPE_EVENT_TYPES:
        return _run_referential(payload, "read_event_types")
    if job_type == jobs.JOB_TYPE_DOCUMENT_CATEGORIES:
        return _run_referential(payload, "read_document_categories")
    if job_type == jobs.JOB_TYPE_SELFTEST:
        return _run_selftest(payload)
    if job_type == jobs.JOB_TYPE_RESET_SESSION:
        return _run_reset_session()
    raise browser_runner.BrowserJobError("unknown_job_type", f"type inconnu : {job_type}")


def _fail(job: dict, code: str, detail: str) -> None:
    job["status"] = "failed"
    job["error_code"] = code
    job["error_detail"] = detail
    job["error"] = code  # champ historique, conservé pour les appelants existants
    jobs.save_job(job)


def _process_job(job: dict) -> None:
    job_id = job["id"]
    payload = job.get("payload") or {}
    is_mutation = job.get("type") not in jobs.READ_JOB_TYPES

    if job.get("mutation_started"):
        logger.warning(f"job_id={job_id} mutation_started already: pas de rejeu")
        _fail(job, "mutation_started_no_rejeu", "une écriture a déjà été engagée pour ce job")
        _cleanup_documents(payload.get("document_paths") or [])
        return

    _set_current_job(job_id)
    job["status"] = "running"
    jobs.save_job(job)

    try:
        job["result"] = _dispatch(job, payload)
        job["status"] = "completed"
        if is_mutation and job.get("idempotency_key"):
            _remember_or_release(job)
        jobs.save_job(job)
        logger.info(f"job_id={job_id} status=completed")
    except browser_runner.BrowserJobError as error:
        logger.warning(f"job_id={job_id} failed code={error.code}")
        _fail(job, error.code, error.detail)
        _release_idempotency(job)
    except Exception as error:
        # Exception tierce : seul le type est conservé, son message peut porter une URL ou du HTML.
        logger.exception(f"job_id={job_id} failed: {type(error).__name__}")
        _fail(job, "internal_error", type(error).__name__)
        _release_idempotency(job)
    finally:
        _set_current_job(None)
        _cleanup_documents(payload.get("document_paths") or [])


def _remember_or_release(job: dict) -> None:
    """Mémorise le résultat, sauf si l'échec est rejouable tel quel.

    Un job peut atteindre `completed` avec `success: false` sans avoir rien écrit — un type
    d'événement introuvable, par exemple. Mémoriser ce résultat rendait la clé inutilisable
    pendant 24 h : l'appelant qui suivait la documentation croyait rejouer et recevait le même
    échec, sans qu'aucun travail ne soit refait.
    """
    key = job["idempotency_key"]
    if job.get("mutation_started"):
        idempotency.store_result(key, job["result"])
        return
    if safety.is_replayable_failure(job.get("result")):
        idempotency.release(key)
        logger.info(f"job_id={job['id']} idempotency_released=true reason=replayable_failure")
        return
    idempotency.store_result(key, job["result"])


def _release_idempotency(job: dict) -> None:
    """Libère la clé si — et seulement si — aucune écriture n'a été engagée.

    Après `mutation_started`, libérer la clé autoriserait un rejeu par-dessus une mutation déjà
    partie : c'est exactement le doublon que l'idempotence existe pour empêcher.
    """
    if job.get("mutation_started"):
        return
    if job.get("idempotency_key"):
        idempotency.release(job["idempotency_key"])


def main() -> int:
    if not jobs.is_async_jobs_enabled():
        logger.error("TS_ASYNC_JOBS_ENABLED/REDIS_URL non configurés : arrêt du worker")
        return 1

    sweep_old_traces()
    _recover_orphan_jobs()
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(stop,), name="heartbeat", daemon=True)
    beat.start()
    logger.info("Worker démarré : propriétaire unique du navigateur")
    try:
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
    finally:
        stop.set()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        session_manager.shutdown()
