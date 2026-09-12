"""Session navigateur partagée : un seul TalentsoftBot réutilisé entre les requêtes.

Repris du DR bot et complété : état `degraded` après échecs de login répétés (protège le
compte technique d'un verrouillage Talentsoft), sauvegarde du storage_state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import config
from . import scraper as scraper_module

logger = logging.getLogger(__name__)


def _safe_bootstrap_detail(error: BaseException) -> str:
    """Détail journalisable d'un échec de bootstrap, sans jamais exposer de contenu tiers.

    Le message n'est conservé que pour les exceptions **que ce dépôt construit lui-même**, et
    dont les messages sont écrits sans secret par contrat (`LoginError`, `SelectorNotFound`).
    Toute autre exception — Playwright en tête — peut porter une URL complète, du HTML ou une
    valeur saisie : seul son type est retenu.

    Sans ce détail, un échec de login est indiagnosticable en production ; avec lui pour
    n'importe quelle exception, les logs deviennent un canal de fuite.
    """
    from .ts_pages import LoginError, SelectorNotFound

    name = type(error).__name__
    if not isinstance(error, LoginError | SelectorNotFound):
        return name
    message = str(error).strip().replace("\n", " ")
    return f"{name}: {message[:200]}" if message else name


class SessionBootstrapError(Exception):
    """Création ou authentification de la session partagée en échec."""


class SessionDegradedError(Exception):
    """Trop d'échecs de login : plus aucune tentative automatique."""


@dataclass
class _SessionState:
    bot: scraper_module.TalentsoftBot
    created_at: float
    last_used_at: float


class SessionManager:
    def __init__(self):
        self._state: _SessionState | None = None
        self.login_count = 0
        self._login_failures: list[float] = []
        self._degraded_reason: str | None = None
        self._invalidate_requested: str | None = None
        self._meta_lock = threading.Lock()

    # --- Propriétés ------------------------------------------------------------------

    @property
    def idle_ttl_seconds(self) -> int:
        return config.session_idle_ttl_seconds()

    @property
    def max_age_seconds(self) -> int:
        return config.session_max_age_seconds()

    def is_degraded(self) -> bool:
        return self._degraded_reason is not None

    def status(self) -> dict:
        """Pour le healthcheck : aucune interaction avec le navigateur."""
        with self._meta_lock:
            return {
                "session_open": self._state is not None,
                "session_authenticated": bool(self._state and self._state.bot._authenticated),
                "login_count": self.login_count,
                "degraded": self.is_degraded(),
                "degraded_reason": self._degraded_reason,
            }

    # --- Cycle de vie ----------------------------------------------------------------

    def _bot_alive(self, bot) -> bool:
        try:
            return bool(bot) and bot.is_alive()
        except Exception:
            return False

    def _should_recycle(self, now: float) -> bool:
        if not self._state:
            return True
        if self._invalidate_requested:
            logger.info(f"Session recycle: invalidation demandée ({self._invalidate_requested})")
            return True
        if not self._bot_alive(self._state.bot):
            logger.info("Session recycle: navigateur mort")
            return True
        if now - self._state.last_used_at > self.idle_ttl_seconds:
            logger.info("Session recycle: idle TTL dépassé")
            return True
        if now - self._state.created_at > self.max_age_seconds:
            logger.info("Session recycle: max-age dépassé")
            return True
        return False

    def _close_state(self) -> None:
        if not self._state:
            return
        try:
            self._state.bot.close()
        except Exception as error:
            logger.error(f"Erreur fermeture session: {type(error).__name__}")
        self._state = None

    def invalidate(self, reason: str = "unspecified") -> None:
        """À appeler depuis le thread navigateur. Depuis un autre thread : request_invalidate."""
        logger.warning(f"session_invalidated_reason={reason}")
        self._invalidate_requested = None
        self._close_state()

    def request_invalidate(self, reason: str) -> None:
        """Thread-safe : la session sera fermée au prochain get_bot (pas d'appel Playwright ici)."""
        logger.warning(f"session_invalidate_requested reason={reason}")
        self._invalidate_requested = reason

    def shutdown(self) -> None:
        logger.info("Arrêt session manager: fermeture du navigateur")
        self._close_state()

    def reset_degraded(self) -> None:
        with self._meta_lock:
            self._login_failures.clear()
            self._degraded_reason = None

    def _record_login_failure(self, reason: str) -> None:
        now = time.time()
        window = config.login_failure_window_seconds()
        with self._meta_lock:
            self._login_failures = [t for t in self._login_failures if now - t < window] + [now]
            if len(self._login_failures) >= config.login_max_failures():
                self._degraded_reason = f"login_failures:{reason}"
                logger.error(f"session_degraded reason={self._degraded_reason}")

    def get_bot(self) -> scraper_module.TalentsoftBot:
        """Retourne un bot prêt et authentifié. À appeler sous browser_slot()."""
        if self.is_degraded():
            raise SessionDegradedError(self._degraded_reason or "degraded")

        now = time.time()
        session_reuse = False
        login_performed = False

        try:
            if self._should_recycle(now):
                self._close_state()
                self._invalidate_requested = None
                bot = scraper_module.TalentsoftBot()
                try:
                    bot.ensure_logged_in()
                except Exception:
                    bot.close()
                    raise
                self.login_count += 1
                login_performed = True
                self._state = _SessionState(bot=bot, created_at=now, last_used_at=now)
            else:
                session_reuse = True
                assert self._state is not None
                was_auth = self._state.bot.is_authenticated()
                self._state.bot.ensure_logged_in()
                if not was_auth:
                    self.login_count += 1
                    login_performed = True
                self._state.last_used_at = now

            with self._meta_lock:
                self._login_failures.clear()
            logger.info(
                f"session_reuse={str(session_reuse).lower()} "
                f"login_performed={str(login_performed).lower()} "
                f"login_count={self.login_count}"
            )
            return self._state.bot
        except Exception as error:
            self._close_state()
            self._record_login_failure(type(error).__name__)
            raise SessionBootstrapError(_safe_bootstrap_detail(error)) from error


session_manager = SessionManager()
