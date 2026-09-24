"""Talentsoft-bot : API FastAPI pilotant le Back Office Talentsoft pour Hippolyte.ai."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TypeVar

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import browser_lock, browser_runner, config, idempotency, jobs, models, safety
from .browser_runner import BrowserJobError
from .scraper import sweep_old_traces
from .session_manager import session_manager

# --- Configuration et initialisation -----------------------------------------------------

load_dotenv()

for _dir in (config.LOGS_DIR, config.TRACES_DIR, config.DATA_DIR, config.UPLOAD_DIR, config.STATE_DIR):
    os.makedirs(_dir, exist_ok=True)

if not config.api_tokens():
    raise ValueError("La variable d'environnement API_TOKEN doit être définie.")
if not config.ts_base_url():
    raise ValueError("La variable d'environnement TS_BASE_URL doit être définie.")
if not config.ts_username() or not config.ts_password():
    raise ValueError("Les variables d'environnement TS_USERNAME et TS_PASSWORD doivent être définies.")

ENABLE_API_DOCS = config.enable_api_docs()
API_VERSION = "0.4.0"

security = HTTPBearer()
T = TypeVar("T")

browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(config.LOGS_DIR, f"api_{datetime.now().strftime('%Y%m%d')}.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

if config.redis_url() and not config.async_jobs_enabled():
    # Configuration typique d un deploiement ou le worker tourne mais ou l api l ignore :
    # les deux ouvrent alors un navigateur sur le meme compte technique, et leurs sessions
    # se deconnectent mutuellement jusqu a l etat degrade.
    logger.warning(
        "config_suspecte REDIS_URL est defini mais TS_ASYNC_JOBS_ENABLED est faux : "
        "si un worker tourne, deux navigateurs se disputeront la meme session Talentsoft"
    )


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Bearer token en comparaison à temps constant. Token précédent accepté (rotation)."""
    for token in config.api_tokens():
        if hmac.compare_digest(credentials.credentials, token):
            return credentials.credentials
    raise HTTPException(status_code=401, detail="Token invalide")


# --- Uploads -------------------------------------------------------------------------------


async def save_upload_file(document: UploadFile) -> str:
    """Sauvegarde sûre : extension, taille, renommage serveur, octets magiques."""
    document_path = safety.build_safe_upload_path(document.filename or "")
    max_size = safety.get_max_upload_size_bytes()
    bytes_written = 0
    try:
        with open(document_path, "wb") as buffer:
            while chunk := await document.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > max_size:
                    raise HTTPException(status_code=413, detail="Fichier trop volumineux")
                buffer.write(chunk)
        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="Fichier vide")
        safety.verify_magic_bytes(document_path)
    except Exception:
        cleanup_file(document_path)
        raise
    os.chmod(document_path, 0o600)
    logger.info(f"upload_saved size={bytes_written} hash={safety.file_hash(document_path)}")
    return document_path


async def save_upload_files(documents: list[UploadFile] | None, original_names: list[str]) -> list[str]:
    paths: list[str] = []
    try:
        for document in documents or []:
            if not document or not document.filename:
                continue
            path = await save_upload_file(document)
            # Nom affiché côté Talentsoft : dérivé du nom d'origine, nettoyé.
            renamed = os.path.join(os.path.dirname(path), safety.safe_display_filename(document.filename))
            renamed = _unique_path(renamed)
            os.replace(path, renamed)
            paths.append(renamed)
            original_names.append(document.filename)
    except Exception:
        for path in paths:
            cleanup_file(path)
        raise
    return paths


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}_{int(time.time() * 1000)}{ext}"


def cleanup_file(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as error:
            logger.error(f"cleanup_failed error={type(error).__name__}")


def cleanup_files(paths: list[str]) -> None:
    for path in paths:
        cleanup_file(path)


def _is_redis_unavailable(error: Exception) -> bool:
    """La file de jobs est-elle injoignable ou refuse-t-elle la connexion ?

    Distinguer ce cas compte pour l'appelant : une file indisponible est une panne
    d'infrastructure passagère, qui mérite un `503` et un rejeu, là où un `500` laisse croire
    à une erreur de traitement. Aucune mutation n'a eu lieu — l'échec précède le navigateur.
    """
    try:
        from redis import exceptions as redis_exceptions
    except Exception:
        return False
    return isinstance(
        error,
        redis_exceptions.ConnectionError
        | redis_exceptions.AuthenticationError
        | redis_exceptions.TimeoutError
        | redis_exceptions.ResponseError,
    )


def raise_generic_server_error(route_name: str, error: Exception):
    if isinstance(error, HTTPException):
        raise error
    if _is_redis_unavailable(error):
        # Le détail nomme la cause sans exposer l'URL de connexion, qui porte le mot de passe.
        logger.error(f"redis_indisponible route={route_name} error={type(error).__name__}")
        raise HTTPException(
            status_code=503,
            detail="File de jobs indisponible : réessayer, ou appeler en mode synchrone",
            headers={"Retry-After": str(browser_lock.get_retry_after_seconds())},
        ) from error
    logger.exception(f"Erreur dans {route_name}: {type(error).__name__}")
    raise HTTPException(status_code=500, detail="Erreur interne du serveur")


# --- Exécution d'un travail navigateur --------------------------------------------------------

# L'API n'ouvre un navigateur QUE lorsqu'elle en est propriétaire (mode mono-processus). Dès que
# la file est active, c'est le worker, et lui seul : voir config.browser_owner() pour le pourquoi.

_HTTP_STATUS_BY_CODE = {
    browser_runner.CODE_BROWSER_BUSY: 503,
    browser_runner.CODE_SESSION_DEGRADED: 503,
    browser_runner.CODE_CANDIDATE_NOT_FOUND: 404,
    browser_runner.CODE_APPLICATION_NOT_ON_OFFER: 404,
    browser_runner.CODE_AMBIGUOUS_CANDIDATE: 409,
}

# Les détails exposés sont écrits ici, jamais repris du message d'exception : celui-ci peut porter
# une URL, du HTML ou une valeur saisie. Le diagnostic reste dans les logs et dans `error_detail`.
_HTTP_DETAIL_BY_CODE = {
    browser_runner.CODE_BROWSER_BUSY: "Browser busy",
    browser_runner.CODE_SESSION_DEGRADED: "Session Talentsoft dégradée : intervention requise",
    browser_runner.CODE_CANDIDATE_NOT_FOUND: "Candidat introuvable",
    browser_runner.CODE_APPLICATION_NOT_ON_OFFER: "Ce candidat n'a pas de candidature sur cette offre",
    browser_runner.CODE_AMBIGUOUS_CANDIDATE: (
        "Plusieurs candidats correspondent à cet email : levée d'ambiguïté requise"
    ),
}


def http_error_for(code: str) -> HTTPException:
    """Traduit un code métier en statut HTTP. Tout code inconnu vaut 500, volontairement."""
    status = _HTTP_STATUS_BY_CODE.get(code, 500)
    detail = _HTTP_DETAIL_BY_CODE.get(code, "Erreur interne du serveur")
    headers = None
    if status == 503:
        retry = "600" if code == browser_runner.CODE_SESSION_DEGRADED else str(browser_lock.get_retry_after_seconds())
        headers = {"Retry-After": retry}
    return HTTPException(status_code=status, detail=detail, headers=headers)


def accepted(job_id: str, status: str = "running") -> JSONResponse:
    """202 : le job continue côté worker, l'appelant le suit sur /jobs/{id}.

    Corps et en-têtes identiques quel que soit le chemin — `?async=1`, attente dépassée, ou rejeu
    d'une clé dont le job tourne encore. La distinction n'apprend rien à l'appelant : dans les
    trois cas il doit interroger `/jobs/{id}`.

    Surtout pas un 5xx ici : à cet instant la mutation est peut-être en cours, et un 5xx
    inviterait l'appelant à rejouer — exactement ce qu'il ne faut jamais faire après une écriture
    engagée.
    """
    return JSONResponse(
        content={"job_id": job_id, "status": status, "poll": f"/jobs/{job_id}"},
        status_code=202,
        headers={"Location": f"/jobs/{job_id}", "Retry-After": str(browser_lock.get_retry_after_seconds())},
    )


# --- Mode mono-processus : l'API possède le navigateur ---------------------------------------


def _run_browser_job(job_type: str, work: Callable[[object], T]) -> T:
    started = time.monotonic()
    try:
        result = browser_runner.run_with_session(
            work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds()
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(f"job_type={job_type} duration_ms={duration_ms} admission_rejected=false")
        return result
    except BrowserJobError as error:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning(f"job_type={job_type} duration_ms={duration_ms} code={error.code}")
        raise http_error_for(error.code) from error


async def run_browser_async(job_type: str, work: Callable[[object], T]) -> T:
    """Admission puis exécution dans l'executor mono-thread : la boucle asyncio reste réactive."""
    if not browser_lock.try_admit():
        logger.warning(f"job_type={job_type} admission_rejected=true inflight={browser_lock.inflight_count()}")
        raise browser_lock.busy_exception()
    loop = asyncio.get_running_loop()
    grace_seconds = config.job_timeout_seconds() + config.browser_admitted_lock_timeout_seconds() + 60
    try:
        future = loop.run_in_executor(browser_executor, lambda: _run_browser_job(job_type, work))
        return await asyncio.wait_for(future, timeout=grace_seconds)
    except TimeoutError as error:
        session_manager.request_invalidate("executor_wait_timeout")
        raise HTTPException(status_code=500, detail="Erreur interne du serveur") from error
    finally:
        browser_lock.release_admit()


# --- Mode worker : l'API empile et attend ----------------------------------------------------


def require_worker_available() -> None:
    """Refuse tout de suite si personne ne dépilera, plutôt que de faire attendre pour rien.

    Un 503 est rejouable par l'appelant ; un job empilé sans worker ne l'est pas, et ses documents
    dormiraient indéfiniment dans uploads/.
    """
    snapshot = jobs.read_worker_status()
    if not snapshot:
        logger.warning("worker_unavailable heartbeat=absent")
        raise HTTPException(
            status_code=503,
            detail="Worker navigateur indisponible : réessayer",
            headers={"Retry-After": str(browser_lock.get_retry_after_seconds())},
        )
    if snapshot.get("degraded"):
        raise http_error_for(browser_runner.CODE_SESSION_DEGRADED)


async def wait_for_job(job_id: str, timeout_seconds: int) -> dict | None:
    """Attend la fin du job hors de la boucle asyncio : l'attente Redis est bloquante.

    Le nombre d'attentes simultanées est borné par le contrôle d'admission, ce qui empêche ces
    threads d'épuiser l'executor par défaut.
    """
    return await asyncio.to_thread(jobs.wait_for_result, job_id, timeout_seconds)


def job_result_or_error(job: dict):
    """Résultat d'un job terminé, ou l'erreur HTTP correspondant à son code."""
    if job.get("status") == "completed":
        return job.get("result")
    raise http_error_for(job.get("error_code") or "internal_error")


async def run_read_job(job_type: str, payload: dict, work: Callable[[object], T]):
    """Exécute une lecture. Retourne (résultat, job_id) : exactement l'un des deux est None.

    Les lectures ne portent jamais de clé d'idempotence et ne marquent jamais de mutation : elles
    sont rejouables sans risque, et passent devant les pushs grâce à la file `ts:jobs:read`.
    """
    if config.browser_owner() == "api":
        return await run_browser_async(job_type, work), None
    require_worker_available()
    if not browser_lock.try_admit():
        logger.warning(f"job_type={job_type} admission_rejected=true inflight={browser_lock.inflight_count()}")
        raise browser_lock.busy_exception()
    try:
        job = jobs.enqueue_job(job_type, payload)
        done = await wait_for_job(job["id"], config.sync_read_wait_timeout_seconds())
    finally:
        browser_lock.release_admit()
    if done is None:
        return None, job["id"]
    return job_result_or_error(done), None


# --- Idempotence ----------------------------------------------------------------------------


def build_idempotency_key(
    provided: str | None,
    candidate_email: str,
    offer_id: str,
    event_type: str | None,
    comment: str | None,
    document_paths: list[str],
) -> str:
    """Cle derivee du contenu, a defaut d'une cle fournie.

    L'email n'entre dans la cle que sous forme d'empreinte : la cle se retrouve dans Redis
    et dans les logs, elle ne doit pas y exposer une donnee personnelle.
    """
    if provided:
        return f"k:{safety.short_hash(provided)}"
    parts = [safety.short_hash(candidate_email), offer_id, event_type or "", safety.short_hash(comment or "")]
    parts.extend(sorted(safety.file_hash(p) for p in document_paths))
    return f"f:{safety.short_hash('|'.join(parts))}"


MAX_DOCUMENT_CATEGORIES = 10


def effective_document_categories(
    document_category: str | None, document_categories: list[str] | None
) -> list[str] | None:
    """Liste ordonnée des catégories de dépôt, telle que le scraper l'essaiera.

    `document_categories` (champ multipart répété) prime ; `document_category` reste la première
    catégorie essayée et le seul champ compris par un bot 0.3.0. Libellés nettoyés, dédoublonnés
    (casse et espaces ignorés), bornés à MAX_DOCUMENT_CATEGORIES. None = aucune catégorie fournie
    (le scraper applique alors TS_DEFAULT_DOCUMENT_CATEGORY).
    """
    from .ts_pages import normalize_text

    candidates = [document_category or ""] + list(document_categories or [])
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        label = (raw or "").strip()
        key = normalize_text(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MAX_DOCUMENT_CATEGORIES:
            break
    return out or None


def _replay_response(job_type: str, replay: dict) -> JSONResponse:
    logger.info(f"job_type={job_type} idempotent_replay=true")
    return JSONResponse(content=replay, status_code=200, headers={"X-Idempotent-Replay": "true"})


def _already_in_progress() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Une requête identique est déjà en cours",
        headers={"Retry-After": str(browser_lock.get_retry_after_seconds())},
    )


async def run_idempotent_update(
    *,
    job_type: str,
    key: str,
    payload: dict,
    work: Callable[[object], dict],
    on_enqueued: Callable[[], None] | None = None,
) -> JSONResponse:
    """Exécute une mutation, chez le propriétaire du navigateur.

    `payload` décrit le travail pour le worker ; `work` fait le même travail en direct quand
    l'API est propriétaire. Les deux doivent rester équivalents.

    `on_enqueued` est appelé une fois le job empilé : c'est là que l'appelant cède la propriété
    des fichiers téléversés au worker, et cesse donc de les supprimer en sortie de requête.
    """
    if config.browser_owner() == "worker":
        return await _run_mutation_via_worker(job_type=job_type, key=key, payload=payload, on_enqueued=on_enqueued)
    return await _run_mutation_in_api(job_type=job_type, key=key, work=work)


def _free_idempotency(key: str) -> None:
    """Libère la clé **et** oublie le job qui lui était associé.

    Les deux vont toujours ensemble : `ts:idem:<clé>` autorise une nouvelle réservation, mais
    `ts:idemjob:<clé>` survivrait 24 h et `enqueue_job` rendrait l'ancien job au lieu d'en créer
    un neuf — le rejeu redeviendrait un no-op silencieux.

    Le chemin synchrone n'empile aucun job, donc l'oubli y est le plus souvent sans effet. Il
    couvre le cas d'un déploiement qui bascule entre mode worker et mode mono-processus, et
    surtout il tient la règle : jamais l'un sans l'autre.
    """
    idempotency.release(key)
    jobs.forget_idempotency_job(key)


async def _run_mutation_in_api(*, job_type: str, key: str, work: Callable[[object], dict]) -> JSONResponse:
    state, replay = idempotency.reserve(key)
    if state == "replay" and replay is not None:
        return _replay_response(job_type, replay)
    if state == "in_progress":
        raise _already_in_progress()
    try:
        payload = await run_browser_async(job_type, work)
    except HTTPException as error:
        # Mutation non démarrée (503, 404, 500 bootstrap) : la clé est libérée pour un rejeu légitime.
        _free_idempotency(key)
        raise error
    except Exception:
        _free_idempotency(key)
        raise
    if safety.is_replayable_failure(payload):
        # Rien n'a été écrit : mémoriser interdirait le rejeu que la documentation promet.
        _free_idempotency(key)
        logger.info(f"job_type={job_type} idempotency_released=true reason=replayable_failure")
    else:
        idempotency.store_result(key, payload)
    return JSONResponse(content=payload, status_code=200)


async def _run_mutation_via_worker(
    *,
    job_type: str,
    key: str,
    payload: dict,
    on_enqueued: Callable[[], None] | None,
) -> JSONResponse:
    """Empile la mutation et attend son résultat, dans la limite du budget d'attente.

    C'est le worker qui appelle `store_result` ou `release` : lui seul sait si une écriture a
    été engagée. Libérer la clé ici sur un échec effacerait cette distinction, et autoriserait
    un rejeu par-dessus une mutation déjà partie.
    """
    require_worker_available()
    if not browser_lock.try_admit():
        logger.warning(f"job_type={job_type} admission_rejected=true inflight={browser_lock.inflight_count()}")
        raise browser_lock.busy_exception()
    try:
        state, replay = idempotency.reserve(key)
        if state == "replay" and replay is not None:
            return _replay_response(job_type, replay)
        if state == "in_progress":
            existing = jobs.job_id_for_idempotency_key(key)
            if existing:
                # Cas courant : l'attente d'un appel précédent a expiré et l'appelant rejoue.
                # Lui rendre son job est exploitable ; un 409 ne l'est pas.
                return accepted(existing)
            raise _already_in_progress()
        try:
            job = jobs.enqueue_job(jobs.JOB_TYPE_UPDATE_APPLICATION, payload, key)
        except Exception:
            # Rien n'a été empilé : la clé ne doit pas rester bloquée jusqu'à son TTL.
            idempotency.release(key)
            jobs.forget_idempotency_job(key)
            raise
        if on_enqueued:
            on_enqueued()
        done = await wait_for_job(job["id"], config.sync_wait_timeout_seconds())
    finally:
        browser_lock.release_admit()
    if done is None:
        return accepted(job["id"])
    return JSONResponse(content=job_result_or_error(done), status_code=200)


def _mutation_payload(
    *,
    candidate_email: str,
    offer_id: str,
    event_type: str | None = None,
    comment: str | None = None,
    event_date: str | None = None,
    document_paths: list[str] | None = None,
    document_category: str | None = None,
    document_categories: list[str] | None = None,
) -> dict:
    """Description d'une mutation, telle que le worker la relira."""
    return {
        "candidate_email": candidate_email,
        "offer_id": offer_id,
        "event_type": event_type,
        "comment": comment,
        "event_date": event_date,
        "document_paths": document_paths or [],
        "document_category": document_category,
        "document_categories": document_categories,
    }


def _update_payload(update_info: dict | None) -> dict:
    if not update_info:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")
    success = safety.actions_succeeded(update_info.get("actions") or {})
    return {"success": success, "update_details": update_info}


# --- Application ----------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    removed = sweep_old_traces()
    if removed:
        logger.info(f"traces_swept count={removed}")
    stop_event = asyncio.Event()

    async def sweeper():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
            except TimeoutError:
                sweep_old_traces()

    task = asyncio.create_task(sweeper())
    yield
    stop_event.set()
    task.cancel()
    session_manager.shutdown()
    browser_executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="Talentsoft-bot",
    description="Pilotage du Back Office Talentsoft pour Hippolyte.ai : événements et pièces jointes sur les candidatures.",
    version=API_VERSION,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
    lifespan=lifespan,
)


def _worker_view() -> tuple[dict, dict]:
    """État du worker et profondeur des files, sans jamais lever.

    Un healthcheck qui tombe avec Redis ne sert à rien : il faut au contraire qu'il dise que
    Redis est injoignable.
    """
    try:
        snapshot = jobs.read_worker_status()
        depths = jobs.queue_depths()
    except Exception as error:
        logger.warning(f"worker_view_failed error={type(error).__name__}")
        return {"alive": False, "reason": "redis_unreachable"}, {}
    if not snapshot:
        return {"alive": False, "reason": "heartbeat_expired"}, depths
    return snapshot, depths


@app.get("/", summary="Statut de l'API")
def read_root():
    """Healthcheck sans prise du mutex navigateur."""
    owner = config.browser_owner()
    base = {
        "status": "ok",
        "service": "talentsoft-bot",
        "version": API_VERSION,
        "browser_owner": owner,
        "browser_busy": browser_lock.is_busy(),
        "browser_inflight": browser_lock.inflight_count(),
    }
    if owner == "api":
        return {**base, "worker": {"alive": False, "reason": "browser_owner=api"}, **session_manager.status()}

    worker, depths = _worker_view()
    # Les champs de session sont recopiés à la racine : les sondes existantes les y cherchent,
    # et elles décrivent toujours la seule session qui existe — celle du worker.
    return {
        **base,
        "worker": worker,
        "queues": depths,
        "session_open": bool(worker.get("session_open")),
        "session_authenticated": bool(worker.get("session_authenticated")),
        "login_count": worker.get("login_count", 0),
        "degraded": bool(worker.get("degraded")),
        "degraded_reason": worker.get("degraded_reason"),
    }


@app.post("/update-application", summary="Événement + documents sur une candidature")
async def update_application(
    candidate_email: str = Form(None, description="Email du candidat (pour le retrouver dans le Back Office)"),
    offer_id: str = Form(None, description="Identifiant de l'offre (pour choisir la bonne candidature)"),
    event_type: str = Form(None, description="Type d'événement (libellé ou code). Défaut : TS_DEFAULT_EVENT_TYPE"),
    comment: str = Form(None, description="Commentaire de l'événement. Sans commentaire, aucun événement n'est créé"),
    event_date: str = Form(None, description="Date de l'événement YYYY-MM-DD (défaut : aujourd'hui)"),
    document_category: str = Form(
        None, description="Catégorie de pièce jointe (libellé ou code). Défaut : TS_DEFAULT_DOCUMENT_CATEGORY"
    ),
    document_categories: list[str] = Form(
        None,
        description="Champ répété : catégories de repli, dans l'ordre. Le dépôt se fait dans la première catégorie libre ; document_category reste la première essayée",
    ),
    documents: list[UploadFile] = File(default=[], description="Pièces jointes (0..n)"),
    idempotency_key: str = Form(None, description="Clé de dédoublonnage fournie par l'appelant"),
    token: str = Depends(verify_token),
    async_mode: int = Query(0, alias="async", description="1 = job Redis (202 + /jobs/{id}), 0 = synchrone"),
):
    document_paths: list[str] = []
    original_names: list[str] = []
    try:
        email = safety.validate_candidate_email(candidate_email)
        offer = safety.validate_offer_id(offer_id)
        clean_comment = safety.sanitize_comment(comment)
        _validate_event_date(event_date)
        document_paths = await save_upload_files(documents, original_names)
        if not clean_comment and not document_paths:
            raise HTTPException(status_code=400, detail="Rien à faire : ni commentaire ni document")

        key = build_idempotency_key(idempotency_key, email, offer, event_type, clean_comment, document_paths)
        categories = effective_document_categories(document_category, document_categories)
        first_category = categories[0] if categories else None

        if async_mode == 1:
            if not jobs.is_async_jobs_enabled():
                raise HTTPException(status_code=400, detail="Jobs asynchrones non activés (TS_ASYNC_JOBS_ENABLED)")
            # La clé est réservée ici aussi : sans cette réservation, un appel synchrone et un
            # job asynchrone portant la même clé pouvaient muter la même candidature en parallèle.
            state, replay = idempotency.reserve(key)
            if state == "replay" and replay is not None:
                return _replay_response("update-application", replay)
            if state == "in_progress":
                existing = jobs.job_id_for_idempotency_key(key)
                if existing:
                    return accepted(existing)
                raise _already_in_progress()
            job = jobs.enqueue_update_application(
                candidate_email=email,
                offer_id=offer,
                event_type=event_type,
                comment=clean_comment,
                event_date=event_date,
                document_paths=document_paths,
                document_category=first_category,
                document_categories=categories,
                idempotency_key=key,
            )
            document_paths = []  # propriété transférée au worker
            return accepted(job["id"], status=job["status"])

        paths_for_job = list(document_paths)

        def work(bot):
            return _update_payload(
                bot.update_application(
                    candidate_email=email,
                    offer_id=offer,
                    event_type=event_type,
                    comment=clean_comment,
                    event_date=event_date,
                    document_paths=paths_for_job,
                    document_category=first_category,
                    document_categories=categories,
                )
            )

        return await run_idempotent_update(
            job_type="update-application",
            key=key,
            payload=_mutation_payload(
                candidate_email=email,
                offer_id=offer,
                event_type=event_type,
                comment=clean_comment,
                event_date=event_date,
                document_paths=paths_for_job,
                document_category=first_category,
                document_categories=categories,
            ),
            work=work,
            # Propriété des fichiers transférée au worker : ne plus les supprimer en sortie.
            on_enqueued=document_paths.clear,
        )
    except Exception as error:
        raise_generic_server_error("/update-application", error)
    finally:
        cleanup_files(document_paths)


@app.post("/applications/events", summary="Creer un evenement avec commentaire")
async def create_event(request: models.EventRequest, token: str = Depends(verify_token)):
    try:
        email = safety.validate_candidate_email(request.candidate_email)
        offer = safety.validate_offer_id(request.offer_id)
        clean_comment = safety.sanitize_comment(request.comment)
        if not clean_comment:
            raise HTTPException(status_code=400, detail="Commentaire vide")
        _validate_event_date(request.event_date)
        key = build_idempotency_key(request.idempotency_key, email, offer, request.event_type, clean_comment, [])

        def work(bot):
            return _update_payload(
                bot.update_application(
                    candidate_email=email,
                    offer_id=offer,
                    event_type=request.event_type,
                    comment=clean_comment,
                    event_date=request.event_date,
                    document_paths=[],
                    document_category=None,
                )
            )

        return await run_idempotent_update(
            job_type="create-event",
            key=key,
            payload=_mutation_payload(
                candidate_email=email,
                offer_id=offer,
                event_type=request.event_type,
                comment=clean_comment,
                event_date=request.event_date,
            ),
            work=work,
        )
    except Exception as error:
        raise_generic_server_error("/applications/events", error)


@app.post("/applications/documents", summary="Ajouter une piece jointe")
async def add_documents(
    candidate_email: str = Form(..., description="Email du candidat"),
    offer_id: str = Form(..., description="Identifiant de l'offre"),
    documents: list[UploadFile] = File(..., description="Piece jointe (une seule : une categorie = un fichier)"),
    document_category: str = Form(None),
    document_categories: list[str] = Form(None, description="Champ répété : catégories de repli, dans l'ordre"),
    idempotency_key: str = Form(None),
    token: str = Depends(verify_token),
):
    document_paths: list[str] = []
    original_names: list[str] = []
    try:
        email = safety.validate_candidate_email(candidate_email)
        offer = safety.validate_offer_id(offer_id)
        document_paths = await save_upload_files(documents, original_names)
        if not document_paths:
            raise HTTPException(status_code=400, detail="Aucun document fourni")
        key = build_idempotency_key(idempotency_key, email, offer, None, None, document_paths)
        paths_for_job = list(document_paths)
        categories = effective_document_categories(document_category, document_categories)
        first_category = categories[0] if categories else None

        def work(bot):
            return _update_payload(
                bot.update_application(
                    candidate_email=email,
                    offer_id=offer,
                    document_paths=paths_for_job,
                    document_category=first_category,
                    document_categories=categories,
                )
            )

        return await run_idempotent_update(
            job_type="add-documents",
            key=key,
            payload=_mutation_payload(
                candidate_email=email,
                offer_id=offer,
                document_paths=paths_for_job,
                document_category=first_category,
                document_categories=categories,
            ),
            work=work,
            on_enqueued=document_paths.clear,
        )
    except Exception as error:
        raise_generic_server_error("/applications/documents", error)
    finally:
        cleanup_files(document_paths)


@app.get("/applications/events", summary="Lire l'historique d'une candidature (lecture seule)")
async def list_events(
    candidate_email: str = Query(..., description="Email du candidat"),
    offer_id: str = Query(..., description="Identifiant de l'offre"),
    token: str = Depends(verify_token),
):
    try:
        email = safety.validate_candidate_email(candidate_email)
        offer = safety.validate_offer_id(offer_id)

        def work(bot):
            return {"offer_id": offer, "events": bot.list_events(email, offer)}

        result, pending_job_id = await run_read_job(
            jobs.JOB_TYPE_LIST_EVENTS, {"candidate_email": email, "offer_id": offer}, work
        )
        if pending_job_id:
            return accepted(pending_job_id)
        return JSONResponse(content=result, status_code=200)
    except Exception as error:
        raise_generic_server_error("/applications/events", error)


_REFERENTIAL_CACHE: dict[str, tuple[float, list]] = {}
_REFERENTIAL_TTL_SECONDS = 3600


async def _referential(name: str, job_type: str, reader, candidate_email: str | None, offer_id: str | None):
    """Lit un referentiel dans le Back Office, avec cache : il change rarement.

    Le referentiel est propre au tenant et ne peut etre lu qu'en ouvrant le formulaire d'une
    candidature : d'ou la candidature temoin (TS_SELFTEST_CANDIDATE_EMAIL / TS_SELFTEST_OFFER_ID).
    """
    cached = _REFERENTIAL_CACHE.get(name)
    if cached and time.time() - cached[0] < _REFERENTIAL_TTL_SECONDS:
        return JSONResponse(content={"values": cached[1], "cached": True})
    email = candidate_email or config.ts_selftest_candidate_email()
    offer = offer_id or config.ts_selftest_offer_id()
    if not email or not offer:
        raise HTTPException(
            status_code=400,
            detail="candidate_email et offer_id requis (ou TS_SELFTEST_CANDIDATE_EMAIL / TS_SELFTEST_OFFER_ID)",
        )
    email = safety.validate_candidate_email(email)
    offer = safety.validate_offer_id(offer)

    def work(bot):
        return {"values": reader(bot, email, offer)}

    result, pending_job_id = await run_read_job(job_type, {"candidate_email": email, "offer_id": offer}, work)
    if pending_job_id:
        return accepted(pending_job_id)
    values = result["values"]
    _REFERENTIAL_CACHE[name] = (time.time(), values)
    return JSONResponse(content={"values": values, "cached": False})


@app.get("/referentials/event-types", summary="Types d'evenement proposes par le Back Office")
async def referential_event_types(
    candidate_email: str = Query(None),
    offer_id: str = Query(None),
    token: str = Depends(verify_token),
):
    try:
        return await _referential(
            "event-types",
            jobs.JOB_TYPE_EVENT_TYPES,
            lambda bot, email, offer: bot.read_event_types(email, offer),
            candidate_email,
            offer_id,
        )
    except Exception as error:
        raise_generic_server_error("/referentials/event-types", error)


@app.get("/referentials/document-categories", summary="Categories de pieces jointes du Back Office")
async def referential_document_categories(
    candidate_email: str = Query(None),
    offer_id: str = Query(None),
    token: str = Depends(verify_token),
):
    try:
        return await _referential(
            "document-categories",
            jobs.JOB_TYPE_DOCUMENT_CATEGORIES,
            lambda bot, email, offer: bot.read_document_categories(email, offer),
            candidate_email,
            offer_id,
        )
    except Exception as error:
        raise_generic_server_error("/referentials/document-categories", error)


@app.post("/selftest", summary="Auto-test lecture seule : login, candidature temoin, selecteurs")
async def selftest(token: str = Depends(verify_token)):
    try:
        email = config.ts_selftest_candidate_email() or None
        offer = config.ts_selftest_offer_id() or None

        def work(bot):
            return bot.selftest(email, offer)

        result, pending_job_id = await run_read_job(
            jobs.JOB_TYPE_SELFTEST, {"candidate_email": email or "", "offer_id": offer or ""}, work
        )
        if pending_job_id:
            # Une sonde ne se suit pas sur /jobs : la supervision attend un verdict, pas un renvoi.
            raise HTTPException(
                status_code=503,
                detail="Auto-test toujours en cours : réessayer",
                headers={"Retry-After": str(browser_lock.get_retry_after_seconds())},
            )
        return JSONResponse(content=result, status_code=200 if result.get("ok") else 503)
    except Exception as error:
        raise_generic_server_error("/selftest", error)


@app.post("/admin/reset-session", summary="Sortir de l'état dégradé et fermer la session navigateur")
async def reset_session(token: str = Depends(verify_token)):
    if config.browser_owner() == "api":
        session_manager.reset_degraded()
        session_manager.request_invalidate("admin_reset")
        return {"ok": True, **session_manager.status()}
    # La session appartient au worker. Volontairement enfilé sur la file de LECTURE et non
    # traité comme une mutation : sortir de l'état dégradé doit rester possible même quand la
    # file de pushs est pleine de jobs qui échouent.
    try:
        job = jobs.enqueue_job(jobs.JOB_TYPE_RESET_SESSION, {})
    except Exception as error:
        raise_generic_server_error("/admin/reset-session", error)
    return JSONResponse(
        content={"ok": True, "job_id": job["id"], "poll": f"/jobs/{job['id']}"},
        status_code=202,
        headers={"Location": f"/jobs/{job['id']}"},
    )


@app.get("/jobs/{job_id}", summary="Statut d'un job async")
def get_job(job_id: str, token: str = Depends(verify_token)):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Les chemins de fichiers temporaires ne sortent pas de l'API.
    payload = dict(job)
    if isinstance(payload.get("payload"), dict):
        payload["payload"] = {k: v for k, v in payload["payload"].items() if k != "document_paths"}
    return payload


def _validate_event_date(value: str | None) -> None:
    if value is None or value == "":
        return
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise HTTPException(status_code=400, detail="event_date invalide (YYYY-MM-DD)") from error
