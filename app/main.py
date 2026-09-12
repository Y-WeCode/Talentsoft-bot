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

from . import browser_lock, config, idempotency, models, safety
from .scraper import ApplicationNotFound, BrowserFatalError, SessionExpired, sweep_old_traces
from .session_manager import SessionBootstrapError, SessionDegradedError, session_manager
from .ts_pages import JobTimeout

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
API_VERSION = "0.1.0"

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


def raise_generic_server_error(route_name: str, error: Exception):
    if isinstance(error, HTTPException):
        raise error
    logger.exception(f"Erreur dans {route_name}: {type(error).__name__}")
    raise HTTPException(status_code=500, detail="Erreur interne du serveur")


# --- Exécution sous mutex navigateur ---------------------------------------------------------


def _get_bot_with_bootstrap_retry():
    """Un seul retry si le bootstrap échoue (aucune mutation encore)."""
    try:
        return session_manager.get_bot()
    except SessionBootstrapError:
        if session_manager.is_degraded():
            raise
        logger.warning("Bootstrap session échoué, invalidate + un seul retry")
        session_manager.invalidate("bootstrap_failed")
        return session_manager.get_bot()


def run_with_session(work: Callable[[object], T], lock_timeout_seconds: float = 2.0) -> T:
    """Exécute work(bot) sous le mutex navigateur.

    - bootstrap : un retry ;
    - navigateur perdu en cours de job : invalidation, aucun rejeu ;
    - session dégradée : 503 sans nouvelle tentative de login.
    """
    with browser_lock.browser_slot(timeout_seconds=lock_timeout_seconds):
        try:
            bot = _get_bot_with_bootstrap_retry()
        except SessionDegradedError as error:
            raise HTTPException(
                status_code=503,
                detail="Session Talentsoft dégradée : intervention requise",
                headers={"Retry-After": "600"},
            ) from error
        except SessionBootstrapError as error:
            session_manager.invalidate("bootstrap_retry_failed")
            if session_manager.is_degraded():
                raise HTTPException(
                    status_code=503,
                    detail="Session Talentsoft dégradée : intervention requise",
                    headers={"Retry-After": "600"},
                ) from error
            raise HTTPException(status_code=500, detail="Erreur interne du serveur") from error

        try:
            return work(bot)
        except BrowserFatalError as error:
            session_manager.invalidate("browser_fatal_mid_job")
            logger.exception(f"BrowserFatalError mid-job: {type(error).__name__}")
            raise HTTPException(status_code=500, detail="Erreur interne du serveur") from error
        except JobTimeout as error:
            session_manager.invalidate("job_timeout")
            logger.error("job_timeout")
            raise HTTPException(status_code=500, detail="Erreur interne du serveur") from error
        except SessionExpired as error:
            session_manager.invalidate("session_expired")
            raise HTTPException(status_code=500, detail="Erreur interne du serveur") from error
        except ApplicationNotFound as error:
            raise HTTPException(status_code=404, detail="Candidature introuvable") from error


def _run_browser_job(job_type: str, work: Callable[[object], T]) -> T:
    started = time.monotonic()
    try:
        result = run_with_session(work, lock_timeout_seconds=config.browser_admitted_lock_timeout_seconds())
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(f"job_type={job_type} duration_ms={duration_ms} admission_rejected=false")
        return result
    except HTTPException as error:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning(f"job_type={job_type} duration_ms={duration_ms} status={error.status_code}")
        raise


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


async def run_idempotent_update(
    *,
    job_type: str,
    key: str,
    work: Callable[[object], dict],
) -> JSONResponse:
    state, replay = idempotency.reserve(key)
    if state == "replay" and replay is not None:
        logger.info(f"job_type={job_type} idempotent_replay=true")
        return JSONResponse(content=replay, status_code=200, headers={"X-Idempotent-Replay": "true"})
    if state == "in_progress":
        raise HTTPException(
            status_code=409,
            detail="Une requête identique est déjà en cours",
            headers={"Retry-After": str(browser_lock.get_retry_after_seconds())},
        )
    try:
        payload = await run_browser_async(job_type, work)
    except HTTPException as error:
        # Mutation non démarrée (503, 404, 500 bootstrap) : la clé est libérée pour un rejeu légitime.
        idempotency.release(key)
        raise error
    except Exception:
        idempotency.release(key)
        raise
    idempotency.store_result(key, payload)
    return JSONResponse(content=payload, status_code=200)


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


@app.get("/", summary="Statut de l'API")
def read_root():
    """Healthcheck sans prise du mutex navigateur."""
    status = session_manager.status()
    return {
        "status": "ok",
        "service": "talentsoft-bot",
        "version": API_VERSION,
        "browser_busy": browser_lock.is_busy(),
        "browser_inflight": browser_lock.inflight_count(),
        **status,
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

        if async_mode == 1:
            from . import jobs as jobs_mod

            if not jobs_mod.is_async_jobs_enabled():
                raise HTTPException(status_code=400, detail="Jobs asynchrones non activés (TS_ASYNC_JOBS_ENABLED)")
            job = jobs_mod.enqueue_update_application(
                candidate_email=email,
                offer_id=offer,
                event_type=event_type,
                comment=clean_comment,
                event_date=event_date,
                document_paths=document_paths,
                document_category=document_category,
                idempotency_key=key,
            )
            document_paths = []  # propriété transférée au worker
            return JSONResponse(content={"job_id": job["id"], "status": job["status"]}, status_code=202)

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
                    document_category=document_category,
                )
            )

        return await run_idempotent_update(job_type="update-application", key=key, work=work)
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

        return await run_idempotent_update(job_type="create-event", key=key, work=work)
    except Exception as error:
        raise_generic_server_error("/applications/events", error)


@app.post("/applications/documents", summary="Ajouter une piece jointe")
async def add_documents(
    candidate_email: str = Form(..., description="Email du candidat"),
    offer_id: str = Form(..., description="Identifiant de l'offre"),
    documents: list[UploadFile] = File(..., description="Piece jointe (une seule : une categorie = un fichier)"),
    document_category: str = Form(None),
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

        def work(bot):
            return _update_payload(
                bot.update_application(
                    candidate_email=email,
                    offer_id=offer,
                    document_paths=paths_for_job,
                    document_category=document_category,
                )
            )

        return await run_idempotent_update(job_type="add-documents", key=key, work=work)
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

        payload = await run_browser_async("list-events", work)
        return JSONResponse(content=payload, status_code=200)
    except Exception as error:
        raise_generic_server_error("/applications/events", error)


_REFERENTIAL_CACHE: dict[str, tuple[float, list]] = {}
_REFERENTIAL_TTL_SECONDS = 3600


async def _referential(name: str, reader, candidate_email: str | None, offer_id: str | None):
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
        return reader(bot, email, offer)

    values = await run_browser_async(f"referential-{name}", work)
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

        payload = await run_browser_async("selftest", work)
        return JSONResponse(content=payload, status_code=200 if payload.get("ok") else 503)
    except Exception as error:
        raise_generic_server_error("/selftest", error)


@app.post("/admin/reset-session", summary="Sortir de l'état dégradé et fermer la session navigateur")
async def reset_session(token: str = Depends(verify_token)):
    session_manager.reset_degraded()
    session_manager.request_invalidate("admin_reset")
    return {"ok": True, **session_manager.status()}


@app.get("/jobs/{job_id}", summary="Statut d'un job async")
def get_job(job_id: str, token: str = Depends(verify_token)):
    from . import jobs as jobs_mod

    job = jobs_mod.get_job(job_id)
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
