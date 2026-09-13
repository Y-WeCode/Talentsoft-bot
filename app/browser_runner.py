"""Exécution d'un travail navigateur, sans dépendre de FastAPI.

Ce module existe pour que le **worker** n'ait plus à importer `app.main`. Auparavant il le
faisait, et récupérait donc des `HTTPException` : un job échoué ne gardait qu'un
« HTTPException » sans code ni cause, inexploitable pour l'appelant. Pire, importer `app.main`
construisait dans le worker toute l'application HTTP.

La traduction en statut HTTP appartient désormais à `app.main` seul ; ici on ne produit que des
codes métier stables, que l'API mappe et que le job persiste.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from . import browser_lock
from . import session_manager as session_module
from .scraper import ApplicationNotFound, BrowserFatalError, SessionExpired
from .session_manager import SessionBootstrapError, SessionDegradedError
from .ts_pages import AmbiguousCandidate, ApplicationNotOnOffer, CandidateNotFound, JobTimeout

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Codes d'erreur métier. Stables : ils sont écrits dans les jobs, lus par l'appelant et mappés
# vers des statuts HTTP par `app.main`. Les renommer casse le contrat.
CODE_SESSION_DEGRADED = "session_degraded"
CODE_SESSION_BOOTSTRAP_FAILED = "session_bootstrap_failed"
CODE_BROWSER_FATAL = "browser_fatal"
CODE_JOB_TIMEOUT = "job_timeout"
CODE_SESSION_EXPIRED = "session_expired"
CODE_CANDIDATE_NOT_FOUND = "candidate_not_found"
CODE_APPLICATION_NOT_ON_OFFER = "application_not_on_offer"
CODE_AMBIGUOUS_CANDIDATE = "ambiguous_candidate"
CODE_BROWSER_BUSY = "browser_busy"


class BrowserJobError(Exception):
    """Échec d'un travail navigateur, porteur d'un code stable et d'un détail journalisable.

    `detail` est destiné à un humain qui diagnostique ; il ne doit jamais être analysé par
    machine — c'est à cela que sert `code`.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _get_bot_with_bootstrap_retry():
    """Un seul retry si le bootstrap échoue (aucune mutation encore)."""
    try:
        return session_module.session_manager.get_bot()
    except SessionBootstrapError as error:
        if session_module.session_manager.is_degraded():
            raise
        logger.warning(f"Bootstrap session échoué ({error}), invalidate + un seul retry")
        session_module.session_manager.invalidate("bootstrap_failed")
        return session_module.session_manager.get_bot()


def run_with_session(work: Callable[[object], T], lock_timeout_seconds: float = 2.0) -> T:
    """Exécute work(bot) sous le mutex navigateur, dans le processus propriétaire.

    - bootstrap : un retry ;
    - navigateur perdu en cours de job : invalidation, aucun rejeu ;
    - session dégradée : échec immédiat, sans nouvelle tentative de login (protège le compte).
    """
    if not browser_lock.try_acquire(lock_timeout_seconds):
        raise BrowserJobError(CODE_BROWSER_BUSY, "un autre travail navigateur est en cours")
    try:
        try:
            bot = _get_bot_with_bootstrap_retry()
        except SessionDegradedError as error:
            raise BrowserJobError(CODE_SESSION_DEGRADED, str(error)[:200]) from error
        except SessionBootstrapError as error:
            session_module.session_manager.invalidate("bootstrap_retry_failed")
            if session_module.session_manager.is_degraded():
                raise BrowserJobError(CODE_SESSION_DEGRADED, str(error)[:200]) from error
            raise BrowserJobError(CODE_SESSION_BOOTSTRAP_FAILED, str(error)[:200]) from error

        try:
            return work(bot)
        except BrowserFatalError as error:
            # Le message porte le texte verbatim de Playwright (URL, HTML) : seul le type sort.
            session_module.session_manager.invalidate("browser_fatal_mid_job")
            logger.exception(f"BrowserFatalError mid-job: {type(error).__name__}")
            raise BrowserJobError(CODE_BROWSER_FATAL, type(error).__name__) from error
        except JobTimeout as error:
            session_module.session_manager.invalidate("job_timeout")
            logger.error("job_timeout")
            raise BrowserJobError(CODE_JOB_TIMEOUT, "délai du job dépassé") from error
        except SessionExpired as error:
            session_module.session_manager.invalidate("session_expired")
            raise BrowserJobError(CODE_SESSION_EXPIRED, "session Talentsoft expirée en cours de job") from error
        except (ApplicationNotFound, CandidateNotFound) as error:
            raise BrowserJobError(CODE_CANDIDATE_NOT_FOUND, "Candidat introuvable") from error
        except ApplicationNotOnOffer as error:
            raise BrowserJobError(
                CODE_APPLICATION_NOT_ON_OFFER,
                "Ce candidat n'a pas de candidature sur cette offre",
            ) from error
        except AmbiguousCandidate as error:
            # Plusieurs candidats pour cet email : on refuse de choisir. Écrire sur le dossier
            # d'un autre candidat serait une divulgation de données personnelles.
            raise BrowserJobError(
                CODE_AMBIGUOUS_CANDIDATE,
                "Plusieurs candidats correspondent à cet email : levée d'ambiguïté requise",
            ) from error
    finally:
        browser_lock.release()
