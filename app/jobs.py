"""File de jobs Redis : le seul chemin entre l'API et le navigateur.

Le worker est le **seul** processus à piloter Chromium (docs/DISCOVERY.md) : le tenant n'admet
qu'une session active par compte, et deux navigateurs concurrents se déconnectaient mutuellement
jusqu'à faire basculer le bot en état dégradé. L'API n'ouvre donc plus de navigateur : elle empile
ici, et attend le résultat.

Deux files, pour que les lectures ne patientent pas derrière les mutations :

- `ts:jobs:read`  — lectures (historique, référentiels, selftest), **prioritaire**
- `ts:jobs:push`  — mutations (événements, pièces jointes)

`brpop` respecte l'ordre des clés qu'on lui passe : une consultation d'historique passe donc devant
vingt pushs en attente.
"""

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

# Clé de complétion, consommée par un BLPOP : l'API est réveillée dès que le worker a fini, sans
# interroger Redis en boucle. Elle ne porte pas le résultat — celui-ci vit dans le job, qui est la
# seule source de vérité et survit à la lecture.
DONE_KEY_PREFIX = "ts:jobdone:"
DONE_TTL_SECONDS = 3600

# Battement de cœur du worker. L'API s'en sert pour refuser proprement (503) quand personne ne
# dépile, plutôt que de laisser l'appelant attendre un résultat qui ne viendra pas.
WORKER_HEARTBEAT_KEY = "ts:worker:heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = 30

# Types de job. Les lectures vont sur QUEUE_READ, les mutations sur QUEUE_PUSH.
JOB_TYPE_UPDATE_APPLICATION = "update-application"
JOB_TYPE_LIST_EVENTS = "list-events"
JOB_TYPE_EVENT_TYPES = "referential-event-types"
JOB_TYPE_DOCUMENT_CATEGORIES = "referential-document-categories"
JOB_TYPE_SELFTEST = "selftest"
JOB_TYPE_RESET_SESSION = "reset-session"

READ_JOB_TYPES = frozenset(
    {
        JOB_TYPE_LIST_EVENTS,
        JOB_TYPE_EVENT_TYPES,
        JOB_TYPE_DOCUMENT_CATEGORIES,
        JOB_TYPE_SELFTEST,
        JOB_TYPE_RESET_SESSION,
    }
)


def queue_for(job_type: str) -> str:
    """File d'un type de job : les lectures passent devant les mutations."""
    return QUEUE_READ if job_type in READ_JOB_TYPES else QUEUE_PUSH


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


def enqueue_job(
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Empile un job et retourne son état initial.

    Une clé d'idempotence déjà connue renvoie le job existant : deux appels identiques ne créent
    jamais deux jobs, et donc jamais deux mutations.
    """
    if not is_async_jobs_enabled():
        raise RuntimeError("Async jobs not enabled")
    r = _redis()
    if r is None:
        raise RuntimeError("REDIS_URL missing")
    queue = queue_for(job_type)

    if idempotency_key:
        existing = r.get(f"{IDEM_JOB_KEY_PREFIX}{idempotency_key}")
        if existing:
            job = get_job(existing)
            # Un job encore en vie (queued/running) est rendu tel quel : deux appels identiques ne
            # créent jamais deux jobs. Un job **terminé** ne l'est jamais : si l'appelant arrive ici,
            # c'est que la clé d'idempotence a été libérée (échec rejouable, ex. `category_occupied`)
            # ou a expiré — rendre l'ancien résultat rendrait le rejeu impossible pendant 24 h, le
            # nouveau payload (autres catégories de repli…) n'étant jamais empilé.
            if job and job.get("status") not in ("completed", "failed"):
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
        # Écrasement volontaire : la clé peut encore pointer vers un job terminé (voir ci-dessus).
        pipe.set(f"{IDEM_JOB_KEY_PREFIX}{idempotency_key}", job_id, ex=JOB_TTL_SECONDS)
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
    document_categories: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "candidate_email": candidate_email,
        "offer_id": offer_id,
        "event_type": event_type,
        "comment": comment,
        "event_date": event_date,
        "document_paths": document_paths,
        "document_category": document_category,
        # Liste ordonnée de catégories : dépôt dans la première libre (0.4.0). None = [document_category].
        "document_categories": document_categories,
    }
    return enqueue_job(JOB_TYPE_UPDATE_APPLICATION, payload, idempotency_key)


def forget_idempotency_job(key: str | None) -> None:
    """Oublie le job associé à une clé d'idempotence libérée.

    À appeler partout où `idempotency.release` l'est : sans cela, `ts:idemjob:<clé>` survivait 24 h à
    la libération et une re-soumission sous la même clé recevait l'ancien job terminé (donc l'ancien
    résultat, ex. `category_occupied`) au lieu d'en créer un nouveau — le rejeu promis par la
    documentation était un no-op silencieux.
    """
    if not key:
        return
    r = _redis()
    if r is None:
        return
    try:
        r.delete(f"{IDEM_JOB_KEY_PREFIX}{key}")
    except Exception as error:  # pragma: no cover - Redis indisponible : le TTL fera le ménage
        logger.warning(f"forget_idempotency_job_failed error={type(error).__name__}")


def job_id_for_idempotency_key(key: str) -> str | None:
    """Job déjà créé sous cette clé d'idempotence, s'il existe.

    Sert à répondre « voici ton job » plutôt que 409 quand l'appelant rejoue après une attente
    expirée : le 409 lui dirait qu'une requête identique tourne, sans lui dire laquelle.
    """
    r = _redis()
    if r is None:
        return None
    return r.get(f"{IDEM_JOB_KEY_PREFIX}{key}")


def get_job(job_id: str) -> dict[str, Any] | None:
    r = _redis()
    if r is None:
        return None
    raw = r.get(f"{JOB_KEY_PREFIX}{job_id}")
    if not raw:
        return None
    return json.loads(raw)


def save_job(job: dict[str, Any]) -> None:
    """Persiste le job, et réveille l'appelant s'il est terminé.

    La clé de complétion n'est poussée que sur un état final : une mise à jour intermédiaire
    (`running`, `mutation_started`) ne doit pas faire croire à un résultat disponible.
    """
    r = _redis()
    if r is None:
        return
    job["updated_at"] = _now_iso()
    pipe = r.pipeline()
    pipe.set(f"{JOB_KEY_PREFIX}{job['id']}", json.dumps(job), ex=JOB_TTL_SECONDS)
    if job.get("status") in ("completed", "failed"):
        pipe.lpush(f"{DONE_KEY_PREFIX}{job['id']}", job["status"])
        pipe.expire(f"{DONE_KEY_PREFIX}{job['id']}", DONE_TTL_SECONDS)
    pipe.execute()


def wait_for_result(job_id: str, timeout_seconds: int) -> dict[str, Any] | None:
    """Attend la fin d'un job. Retourne le job terminé, ou None si le délai est dépassé.

    Bloque sur la clé de complétion plutôt que d'interroger Redis en boucle : la réponse part dès
    que le worker a fini, sans latence de scrutation ni charge inutile.

    Un `None` ne signifie pas l'échec : le job continue côté worker, et l'appelant reçoit son
    identifiant pour le suivre.
    """
    r = _redis()
    if r is None:
        return None
    # Le job a pu se terminer avant que l'on commence à attendre.
    job = get_job(job_id)
    if job and job.get("status") in ("completed", "failed"):
        return job
    if r.blpop(f"{DONE_KEY_PREFIX}{job_id}", timeout=max(1, timeout_seconds)) is None:
        # Le jeton de complétion n'est poussé qu'une fois : si deux appels attendent le même job,
        # le second ne sera pas réveillé. Une relecture lève cette ambiguïté sans coût.
        job = get_job(job_id)
        return job if job and job.get("status") in ("completed", "failed") else None
    return get_job(job_id)


def worker_heartbeat(status: dict[str, Any] | None = None) -> None:
    """Signale que le worker est vivant, avec l'état de sa session.

    Sans ce battement, l'API ne peut pas distinguer « personne ne dépile » d'un job simplement
    lent — et laisserait l'appelant attendre un résultat qui ne viendrait jamais.
    """
    r = _redis()
    if r is None:
        return
    payload = {"at": _now_iso(), **(status or {})}
    r.set(WORKER_HEARTBEAT_KEY, json.dumps(payload), ex=WORKER_HEARTBEAT_TTL_SECONDS)


def read_worker_status() -> dict[str, Any] | None:
    """État publié par le worker, ou None si son battement a expiré (worker absent ou bloqué)."""
    r = _redis()
    if r is None:
        return None
    raw = r.get(WORKER_HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def queue_depths() -> dict[str, int]:
    """Profondeur des deux files, pour la supervision."""
    r = _redis()
    if r is None:
        return {}
    try:
        return {"read": int(r.llen(QUEUE_READ)), "push": int(r.llen(QUEUE_PUSH))}
    except Exception:
        return {}


def brpop_next_job(timeout_seconds: int = 5) -> tuple[str, str] | None:
    r = _redis()
    if r is None:
        return None
    # Ordre significatif : les lectures passent avant les mutations.
    item = r.brpop([QUEUE_READ, QUEUE_PUSH], timeout=timeout_seconds)
    if not item:
        return None
    queue_name, job_id = item
    return queue_name, job_id
