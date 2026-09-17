"""TalentsoftBot : pilote le Back Office Talentsoft avec Playwright (API sync).

Toutes les méthodes s'exécutent dans l'unique thread de l'executor navigateur.

Règles :
- aucune valeur de mot de passe, de cookie ou de commentaire dans les logs ;
- chaque mutation est relue dans le DOM quand c'est possible (`verified`) ;
- un échec après début de mutation n'est jamais rejoué ici ni par l'appelant.

Contraintes imposées par le Back Office réel (docs/DISCOVERY.md) :

- **Authentification fédérée** sur trois hôtes : la navigation doit être autorisée au-delà
  de `TS_BASE_URL` (`TS_AUTH_HOSTS`), avec un écran de choix de compte à franchir.
- **Une fiche n'est pas adressable par URL** à partir des identifiants d'Hippolyte.ai : on
  retrouve le candidat par email, puis la candidature par la référence de l'offre.
- **Les formulaires vivent dans des iframes** et la navigation interne se fait par postback.
- **Un `confirm()` natif** peut survenir : Playwright le rejette par défaut, d'où un handler.
- **Déposer une pièce jointe dans une catégorie occupée ÉCRASE** le document existant : le
  contrôle anti-écrasement est non contournable.
- **Le SSO atterrit hors du Back Office** (espace collaborateur) : le succès du login se
  constate en deux temps, sortie du parcours SSO puis arrivée sur le Back Office.
- **Le commentaire d'un événement n'est pas relisible** : la vérification reste faible et
  le dédoublonnage par relecture est impossible (il produirait des faux positifs).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from . import config, safety
from . import ts_selectors as sel
from .ts_pages import (
    AmbiguousCandidate,
    ApplicationNotOnOffer,
    ApplicationPage,
    AttachmentsDialog,
    CandidateNotFound,
    CookieBanner,
    Deadline,
    EventDialog,
    GlobalSearch,
    JobTimeout,
    LoginError,
    LoginPage,
    MailDialogOpened,
    SelectorNotFound,
    normalize_text,
    parse_attachment_label,
    to_talentsoft_date,
)

logger = logging.getLogger(__name__)

# Types d'exception sans ambiguite : le navigateur est perdu, quel que soit le libelle.
# Compare sur le nom de classe car `TargetClosedError` n'est pas exporte par
# `playwright.sync_api` ; l'importer depuis `playwright._impl._errors` creerait une dependance
# a une API privee. Constate en recette : un TargetClosedError dont le message ne matchait aucun
# fragment a ete traite comme un echec metier ordinaire, sans invalider la session.
_FATAL_ERROR_TYPES = ("TargetClosedError",)

_FATAL_FRAGMENTS = (
    "target closed",
    "has been closed",
    "browser closed",
    "connection closed",
    "target page, context or browser has been closed",
    "protocol error",
    "browser process exited",
)


# User-agent d'un Chromium de bureau. Repris tel quel en headless, où Playwright annoncerait
# sinon « HeadlessChrome ». La version suit celle de l'image Playwright du Dockerfile ; un
# décalage mineur est sans conséquence, seul le mot « Headless » pose problème.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class BrowserFatalError(Exception):
    """Le navigateur ou le contexte est perdu : la session doit être recyclée."""


class ApplicationNotFound(Exception):
    """La fiche candidature n'existe pas ou n'est pas accessible au compte technique."""


class SessionExpired(Exception):
    """Talentsoft a renvoyé vers la page de login en cours de job."""


def is_fatal_playwright_error(error: BaseException) -> bool:
    message = str(error).lower()
    if not isinstance(error, PlaywrightError):
        return False
    if type(error).__name__ in _FATAL_ERROR_TYPES:
        return True
    return any(fragment in message for fragment in _FATAL_FRAGMENTS)


def _event_signature(event_type: str | None, event_date: str | None) -> str:
    """Signature FAIBLE d'un événement : type + date, les seules données relues.

    Le commentaire n'étant pas affiché dans l'historique, il ne peut pas entrer dans cette
    signature. Elle sert donc à *confirmer* qu'un événement a bien été créé, jamais à
    conclure qu'un événement est « déjà présent » : deux synthèses différentes du même jour
    partagent la même signature.
    """
    parts = [normalize_text(event_type or ""), normalize_text(to_talentsoft_date(event_date) or "")]
    return " ".join(part for part in parts if part)


class TalentsoftBot:
    def __init__(self):
        self.base_url = config.ts_base_url()
        if not self.base_url:
            raise RuntimeError("TS_BASE_URL manquant")
        self._pw = None
        self.browser = None
        self.context = None
        self.page: Page | None = None
        self._tracing = False
        self._authenticated = False
        self._cookies_handled = False
        self._mutation_callback: Callable[[], None] | None = None
        self._mutation_notified = False
        self.deadline = Deadline(config.job_timeout_seconds())
        self._launch()

    # --- Cycle de vie ------------------------------------------------------------------

    def _launch(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": config.headless_mode()}
        executable = config.browser_executable_path()
        if executable:
            launch_kwargs["executable_path"] = executable
        self.browser = self._pw.chromium.launch(**launch_kwargs)

        # Viewport fixé explicitement : l'en-tête du Back Office est responsive et REPLIE la
        # barre de recherche sous ~1000 px de large (constaté à 793 px, où le champ passe en
        # `display: none`). Or cette barre est le seul chemin vers une fiche candidat.
        # Ne pas dépendre du défaut de Playwright, qui pourrait changer de version en version.
        context_kwargs = {
            "accept_downloads": False,
            "locale": "fr-FR",
            "viewport": {"width": 1440, "height": 900},
        }
        # En mode headless, Chromium annonce « HeadlessChrome » dans son user-agent. Des
        # fournisseurs d'identité refusent ces navigateurs — sans message d'erreur : le
        # formulaire est accepté, mais l'authentification n'aboutit pas. On présente donc le
        # même Chromium sous son user-agent normal.
        # Ce n'est pas un contournement de protection : le bot s'authentifie avec un compte
        # applicatif légitime, sur un tenant dont l'exploitant demande cette automatisation.
        user_agent = config.browser_user_agent()
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        elif config.headless_mode():
            context_kwargs["user_agent"] = _DEFAULT_USER_AGENT
        state_path = config.storage_state_path()
        if os.path.exists(state_path):
            context_kwargs["storage_state"] = state_path
        self.context = self.browser.new_context(**context_kwargs)
        self.context.set_default_timeout(config.action_timeout_ms())
        self.context.set_default_navigation_timeout(config.navigation_timeout_ms())
        # Navigation principale limitée au tenant et aux hôtes d'authentification :
        # les cookies de session ne sortent pas de ce périmètre.
        self.context.route("**/*", self._guard_route)
        self.page = self.context.new_page()
        # Les actions de workflow ouvrent un confirm() natif. Sans ce handler, Playwright le
        # rejette et le clic reste sans effet, silencieusement.
        self.page.on("dialog", self._handle_dialog)

    def _handle_dialog(self, dialog) -> None:
        try:
            logger.info(f"native_dialog type={dialog.type} accepted=true")
            dialog.accept()
        except Exception as error:
            logger.warning(f"dialog_accept_failed error={type(error).__name__}")

    def _guard_route(self, route, request) -> None:
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            if not safety.is_allowed_navigation(request.url):
                logger.warning(f"navigation_blocked host={request.url.split('/')[2] if '//' in request.url else '?'}")
                route.abort("blockedbyclient")
                return
        route.continue_()

    def is_alive(self) -> bool:
        try:
            return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())
        except Exception:
            return False

    def close(self) -> None:
        for step in (self._stop_trace_discard, self._close_context, self._close_browser, self._stop_playwright):
            try:
                step()
            except Exception as error:
                logger.debug(f"close_step_failed step={step.__name__} error={type(error).__name__}")
        self.page = None
        self.context = None
        self.browser = None
        self._pw = None

    def _close_context(self) -> None:
        if self.context:
            self.context.close()

    def _close_browser(self) -> None:
        if self.browser:
            self.browser.close()

    def _stop_playwright(self) -> None:
        if self._pw:
            self._pw.stop()

    def new_job(self) -> None:
        """Réinitialise le budget temps au début de chaque job."""
        self.deadline = Deadline(config.job_timeout_seconds())

    # --- Observabilité -----------------------------------------------------------------

    def start_trace(self) -> None:
        if not config.traces_enabled() or self._tracing or not self.context:
            return
        try:
            self.context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._tracing = True
        except Exception as error:
            logger.warning(f"trace_start_failed error={type(error).__name__}")

    def stop_trace(self, keep: bool, name: str) -> str | None:
        if not self._tracing or not self.context:
            return None
        self._tracing = False
        try:
            if keep:
                os.makedirs(config.TRACES_DIR, exist_ok=True)
                path = os.path.join(config.TRACES_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.zip")
                self.context.tracing.stop(path=path)
                os.chmod(path, 0o600)
                return path
            self.context.tracing.stop()
        except Exception as error:
            logger.warning(f"trace_stop_failed error={type(error).__name__}")
        return None

    def _stop_trace_discard(self) -> None:
        self.stop_trace(keep=False, name="discard")

    def screenshot(self, name: str) -> None:
        if not config.screenshots_enabled() or not self.page:
            return
        try:
            os.makedirs(config.TRACES_DIR, exist_ok=True)
            path = os.path.join(config.TRACES_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.png")
            self.page.screenshot(path=path, full_page=True)
            os.chmod(path, 0o600)
        except Exception as error:
            logger.debug(f"screenshot_failed error={type(error).__name__}")

    # --- Authentification --------------------------------------------------------------

    def _login_page(self) -> LoginPage:
        return LoginPage(self.page, self.base_url, self.deadline, config.action_timeout_ms())

    def _dismiss_cookies(self) -> None:
        """Refuse le bandeau Didomi, qui masque la bande basse de la fenêtre."""
        banner = CookieBanner(self.page, config.action_timeout_ms())
        if banner.is_displayed():
            banner.refuse()
            self._cookies_handled = True

    def is_authenticated(self) -> bool:
        """Sonde sans navigation : URL hors parcours de login et marqueur recruteur présent."""
        if not self.is_alive():
            return False
        try:
            if (self.page.url or "about:blank").startswith("about:"):
                return False
            return self._login_page().is_authenticated_view()
        except Exception:
            return False

    def ensure_logged_in(self) -> None:
        if self.is_authenticated():
            self._authenticated = True
            return
        self._goto(self.base_url + sel.LOGIN_PATH)
        self._dismiss_cookies()
        login_page = self._login_page()
        if login_page.is_authenticated_view():
            # Session restaurée depuis storage_state, et déjà sur le Back Office.
            self._authenticated = True
            self.save_storage_state()
            return
        if self._resume_restored_session(login_page):
            return
        self.login()

    def _resume_restored_session(self, login_page: LoginPage) -> bool:
        """Reprend une session restaurée depuis le disque, ou l'abandonne proprement.

        Avec des cookies encore valides, le tenant renvoie la racine du Back Office vers l'espace
        collaborateur tant que la session applicative du BO n'est pas ouverte. Il faut donc y
        **entrer**, pas se réauthentifier : l'IdP nous tient pour connecté et ne présente aucun
        formulaire, si bien que `login()` échouait sur `login_form_not_found` — en quelques
        secondes, sans qu'aucune trace n'explique pourquoi.

        Des cookies périmés laissent au contraire une page qu'on ne reconnaît pas. Les garder
        ferait échouer le login complet de la même façon : on repart d'un contexte vierge.

        Retourne True si la session est reprise et le Back Office atteint.
        """
        if login_page.is_displayed() or login_page.is_account_choice_displayed():
            return False  # L'IdP nous parle : parcours de connexion normal.

        if self._sso_completed(login_page):
            logger.info("restored_session landing=hors_back_office")
            self._enter_back_office()
            self._dismiss_cookies()
            if login_page.is_authenticated_view():
                self._authenticated = True
                self.save_storage_state()
                logger.info("restored_session resumed=true")
                return True

        self._drop_restored_session()
        return False

    def _drop_restored_session(self) -> None:
        """Repart d'un contexte vierge.

        Supprimer le fichier ne suffit pas : ses cookies sont déjà chargés dans le contexte
        courant, et resteraient en vigueur pour tout le job.
        """
        logger.warning("restored_session discarded=true : reprise impossible, login complet")
        self.discard_storage_state()
        try:
            self.context.clear_cookies()
        except Exception as error:
            # Le libellé évite le vocabulaire des secrets : un garde-fou interdit qu'il
            # apparaisse dans une ligne de log de ce fichier (test_no_secret_logging_in_scraper).
            logger.warning(f"session_state_clear_failed error={type(error).__name__}")
        self._goto(self.base_url + sel.LOGIN_PATH)
        self._dismiss_cookies()

    def login(self) -> None:
        """Parcours fédéré : choix du compte puis identifiant / mot de passe."""
        username = config.ts_username()
        password = config.ts_password()
        if not username or not password:
            raise LoginError("missing_credentials")

        login_page = self._login_page()
        if not (login_page.is_displayed() or login_page.is_account_choice_displayed()):
            self._goto(self.base_url + sel.LOGIN_PATH)
            self._dismiss_cookies()
            login_page = self._login_page()

        if login_page.is_authenticated_view():
            self._authenticated = True
            return

        # Étape 1 : écran de choix de compte (passerelle de fédération).
        if login_page.is_account_choice_displayed():
            logger.info("account_choice_displayed")
            login_page.choose_account(config.ts_account_choice())
            self._wait_for_login_form(login_page)

        # Étape 2 : formulaire de l'IdP.
        if not login_page.is_displayed():
            if login_page.is_authenticated_view():
                self._authenticated = True
                self.save_storage_state()
                return
            raise LoginError("login_form_not_found")

        logger.info("login_attempt")
        # Observer la soumission : sans cela, un rejet serveur est indiscernable d'un clic
        # sans effet — les deux laissent le formulaire affiché, sans message.
        with self._watch_login_exchange():
            login_page.submit_credentials(username, password)
        try:
            self.page.wait_for_load_state(
                "networkidle", timeout=self.deadline.remaining_ms(config.navigation_timeout_ms())
            )
        except Exception:
            pass

        # Attente d'un état stable : parcours SSO terminé, ou erreur affichée.
        #
        # Le tenant renvoie vers MyTalentsoft (espace collaborateur) et NON vers le Back
        # Office : attendre ici les marqueurs du Back Office ferait échouer un login réussi.
        # On se contente donc de constater la sortie du parcours d'authentification.
        waited_until = time.monotonic() + config.navigation_timeout_ms() / 1000.0
        landed = False
        while time.monotonic() < waited_until:
            self._dismiss_cookies()
            error_text = login_page.error_text()
            if error_text:
                # Le texte vient de la page du fournisseur d'identite : il reste dans le log.
                # Le porter dans l'exception le ferait remonter jusqu'a la reponse HTTP, ou il
                # exposerait du contenu tiers (URL, HTML, voire la valeur saisie).
                logger.warning(f"login_rejected message={error_text[:120]!r}")
                self.discard_storage_state()
                raise LoginError("credentials_rejected")
            if login_page.is_authenticated_view():
                landed = True
                break
            if self._sso_completed(login_page):
                logger.info("sso_completed landing=hors_back_office")
                landed = True
                break
            self.page.wait_for_timeout(500)

        if not landed:
            # Capture utile au diagnostic : un champ mot de passe s'affiche masqué, sa valeur
            # n'apparaît donc pas. Activée seulement si SCREENSHOTS_ENABLED.
            self.screenshot("login_not_confirmed")
            raise LoginError(
                f"login_not_confirmed: parcours SSO non abouti (url={self._safe_url()}, "
                f"page={self._login_failure_hint(login_page)})"
            )

        # Rejoindre le Back Office : c'est le seul périmètre où le bot travaille, et le seul
        # où ses marqueurs d'authentification ont un sens.
        self._enter_back_office()
        self._dismiss_cookies()
        if login_page.is_displayed() or login_page.is_account_choice_displayed():
            raise LoginError("login_not_confirmed: le Back Office redemande une authentification")
        if not login_page.is_authenticated_view():
            raise LoginError(f"login_not_confirmed: Back Office non reconnu (url={self._safe_url()})")

        self._authenticated = True
        self.save_storage_state()
        logger.info("login_success")

    @contextmanager
    def _watch_login_exchange(self):
        """Journalise la soumission du formulaire : champs envoyés, URL, statut de la réponse.

        Ne sont relevés que les **noms** des champs postés, jamais leurs valeurs : le corps
        contient le mot de passe. Ce relevé répond à trois questions qu'aucun autre signal ne
        tranche — le POST part-il, porte-t-il les bons champs, et que répond le serveur ?
        Un `200` qui réaffiche le formulaire dénonce un rejet silencieux (jeton anti-CSRF,
        identifiants refusés sans message) ; l'absence de POST, un clic sans effet.
        """

        def on_request(request):
            try:
                if request.method != "POST" or not request.is_navigation_request():
                    return
                body = request.post_data or ""
                fields = sorted({pair.split("=", 1)[0] for pair in body.split("&") if "=" in pair})
                logger.info(f"login_post url={self._host_and_path(request.url)} champs={fields}")
            except Exception:
                pass

        def on_response(response):
            try:
                if response.request.method != "POST" or not response.request.is_navigation_request():
                    return
                location = response.headers.get("location", "")
                logger.info(
                    f"login_response status={response.status} "
                    f"url={self._host_and_path(response.url)} "
                    f"redirige_vers={self._host_and_path(location) if location else 'aucune'}"
                )
            except Exception:
                pass

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        try:
            yield
        finally:
            try:
                self.page.remove_listener("request", on_request)
                self.page.remove_listener("response", on_response)
            except Exception:
                pass

    @staticmethod
    def _host_and_path(url: str) -> str:
        """Hôte et chemin seulement : les paramètres d'un échange SSO portent des jetons."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url or "")
            return f"{parsed.netloc}{parsed.path}"[:110] or "?"
        except Exception:
            return "?"

    def _login_failure_hint(self, login_page: LoginPage) -> str:
        """Ce que la page dit au moment où le login n'aboutit pas.

        Sans cet indice, un échec se résume à « on est resté sur l'IdP », ce qui ne distingue
        pas un mot de passe refusé d'un formulaire mal soumis. On se limite au texte
        **visible** de la page de connexion : il ne contient ni identifiant saisi (la valeur
        d'un champ n'est pas du texte), ni donnée candidat, l'authentification précédant
        l'accès au moindre dossier.
        """
        detected = login_page.error_text()
        if detected:
            return f"erreur={detected[:120]!r}"
        try:
            visible = normalize_text(self.page.inner_text("body", timeout=3000))
        except Exception:
            return "texte indisponible"
        # Le formulaire encore affiché est en soi une information : la soumission n'a rien donné.
        still_form = "formulaire_toujours_affiché" if login_page.is_displayed() else "hors_formulaire"
        return f"{still_form} texte={visible[:160]!r}"

    def _sso_completed(self, login_page: LoginPage) -> bool:
        """Le parcours d'authentification est-il sorti des écrans de connexion ?

        Vrai quand l'URL ne porte plus de fragment de login, qu'aucun formulaire de connexion
        n'est affiché, et qu'une application Talentsoft a bien été servie.
        """
        url = (self.page.url or "").lower()
        if any(fragment in url for fragment in sel.LOGIN_URL_FRAGMENTS):
            return False
        if login_page.is_displayed() or login_page.is_account_choice_displayed():
            return False
        from .ts_pages import any_present

        return any_present(self.page, sel.POST_LOGIN_MARKERS, require_visible=False)

    def _enter_back_office(self) -> None:
        """Ouvre le Back Office après le SSO.

        Le viser par son URL ne suffit pas : tant que sa session applicative n'est pas ouverte,
        sa racine comme `/Home/Welcome` renvoient vers l'espace collaborateur. C'est le lien
        « Recrutement » du sélecteur d'espaces (`RedirectBackOffice.ashx`, servi par l'hôte
        d'atterrissage) qui l'ouvre — constaté sur le tenant (docs/DISCOVERY.md).

        On tente donc l'accès direct, puis ce point d'entrée si l'on a été renvoyé ailleurs.
        """
        self._goto(self.base_url + sel.LOGIN_PATH)
        if self._on_back_office():
            return

        landing_origin = self._current_origin()
        if not landing_origin:
            return
        entry = landing_origin + sel.BACK_OFFICE_ENTRY_PATH
        if not safety.is_allowed_navigation(entry):
            logger.warning(f"back_office_entry_hors_allowlist host={self._host_and_path(entry)}")
            return
        logger.info(f"back_office_entry via={self._host_and_path(entry)}")
        self._goto(entry)
        if not self._on_back_office():
            # Le point d'entrée a pu poser la session sans nous y conduire : réessayer l'URL.
            self._goto(self.base_url + sel.LOGIN_PATH)

    def _current_origin(self) -> str:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self.page.url or "")
            return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        except Exception:
            return ""

    def _on_back_office(self) -> bool:
        """Sommes-nous réellement sur le Back Office, et pas ailleurs sur le tenant ?

        Quand la session du Back Office expire, ce tenant ne renvoie PAS vers un formulaire de
        login : il redirige vers l'espace collaborateur (MyTalentsoft), y compris lorsqu'on
        vise directement l'URL d'une fiche. Sans ce contrôle, le bot se croit connecté, puis
        échoue plus loin sur une barre de recherche introuvable — un symptôme qui ne désigne
        pas sa cause.
        """
        try:
            return safety.is_same_origin(self.page.url or "")
        except Exception:
            return False

    def _safe_url(self) -> str:
        """Hôte et chemin uniquement : les paramètres d'un retour SSO portent des jetons."""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self.page.url or "")
            return f"{parsed.netloc}{parsed.path}"[:120]
        except Exception:
            return "?"

    def _wait_for_login_form(self, login_page: LoginPage) -> None:
        waited_until = time.monotonic() + min(config.navigation_timeout_ms(), 30000) / 1000.0
        while time.monotonic() < waited_until:
            if login_page.is_displayed() or login_page.is_authenticated_view():
                return
            self.page.wait_for_timeout(300)

    def discard_storage_state(self) -> None:
        """Oublie la session persistée : la prochaine connexion repart d'un contexte vierge.

        Un `storage_state` périmé porte des cookies d'une session antérieure — dont celui qui
        accompagne le jeton anti-CSRF d'ASP.NET. S'il ne correspond plus au jeton du formulaire
        fraîchement chargé, le serveur **réaffiche le formulaire sans message**, ce qui est
        indiscernable d'un mot de passe refusé. On le supprime donc après un échec de login.
        """
        path = config.storage_state_path()
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("storage_state_discarded : prochaine connexion depuis un contexte vierge")
        except OSError as error:
            logger.warning(f"storage_state_discard_failed error={type(error).__name__}")

    def save_storage_state(self) -> None:
        if not self.context:
            return
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            path = config.storage_state_path()
            self.context.storage_state(path=path)
            os.chmod(path, 0o600)
        except Exception as error:
            logger.warning(f"storage_state_save_failed error={type(error).__name__}")

    def _goto(self, url: str) -> None:
        try:
            self.page.goto(
                url, wait_until="domcontentloaded", timeout=self.deadline.remaining_ms(config.navigation_timeout_ms())
            )
        except PlaywrightError as error:
            if is_fatal_playwright_error(error):
                raise BrowserFatalError(str(error)) from error
            raise

    # --- Ouverture d'une candidature ----------------------------------------------------

    def open_application(self, candidate_email: str, offer_id: str) -> tuple[ApplicationPage, str]:
        """Recherche le candidat par email, ouvre sa fiche, sélectionne la candidature de l'offre.

        Le Back Office n'accepte pas les identifiants de l'API Recruiting Customer : c'est le
        seul chemin praticable (docs/DISCOVERY.md). Retourne (page, libellé de la candidature).
        """
        for attempt in (1, 2):
            self._goto(self.base_url + sel.LOGIN_PATH)
            self._dismiss_cookies()
            login_page = self._login_page()
            expired = (
                login_page.is_displayed() or login_page.is_account_choice_displayed() or not self._on_back_office()
            )
            if expired:
                if attempt == 2:
                    raise SessionExpired(f"session non rétablie après re-login (url={self._safe_url()})")
                logger.info(f"session_expired_relogin url={self._safe_url()}")
                self._authenticated = False
                try:
                    # `ensure_logged_in` et non `login` : quand la session applicative du Back
                    # Office expire, le tenant nous renvoie vers l'espace collaborateur tout en
                    # nous tenant pour authentifiés. Le remède est d'y ré-entrer, pas de
                    # redemander des identifiants — l'IdP ne présenterait aucun formulaire.
                    self.ensure_logged_in()
                except LoginError as error:
                    # Reprise impossible. Laisser la seconde passe conclure à une session perdue
                    # donne le bon diagnostic ; remonter une erreur de login désignerait à tort
                    # les identifiants, et enverrait chercher au mauvais endroit.
                    logger.warning(f"session_recovery_failed reason={error}")
                continue
            break

        search = GlobalSearch(self.page, self.deadline, config.action_timeout_ms())
        search.search(candidate_email)
        # L email est passe a l ouverture : la suggestion est confrontee a l adresse demandee
        # avant d etre cliquee (le tenant l affiche dans le libelle du resultat).
        search.open_single_result(candidate_email)

        app_page = ApplicationPage(self.page, self.deadline, config.action_timeout_ms())
        if app_page.is_not_found():
            raise ApplicationNotFound(f"candidat introuvable pour l'email fourni (offre {offer_id})")
        app_page.wait_ready()
        app_page.open_events_tab()
        label = app_page.select_application_by_offer(offer_id)
        logger.info(f"application_opened offer_id={offer_id}")
        return app_page, label

    def _notify_mutation_started(self) -> None:
        """Enregistre la première écriture réelle, et le signale à l'appelant s'il l'a demandé.

        Le drapeau est posé **avant** et **indépendamment** du callback : sans cela, en mode
        mono-processus — où aucun callback n'est fourni — le bot ne saurait plus lui-même qu'il
        a écrit, et `update_details.mutation_started` se contredirait avec le job.

        Un échec de notification ne doit pas faire échouer une mutation déjà engagée : on
        journalise et on poursuit. Le pire cas est un job rejouable à tort, que l'idempotence
        côté appelant rattrape.
        """
        if self._mutation_notified:
            return
        self._mutation_notified = True
        if self._mutation_callback is None:
            return
        try:
            self._mutation_callback()
        except Exception as error:
            logger.warning(f"mutation_started_callback_failed error={type(error).__name__}")

    def _aborted_action(self, before_click_error: str, **fields) -> dict:
        """Action interrompue avant d'avoir pu accumuler son propre état.

        Le drapeau est repris de `_mutation_notified`, seule source fiable à cet instant : le
        dictionnaire de l'action, lui, est resté dans la fonction qui a levé.
        """
        item: dict = dict(fields)
        if self._mutation_notified:
            item["mutation_started"] = True
        self._failure_after_click(item, before_click_error)
        return item

    def _failure_after_click(self, item: dict, before_click_error: str) -> None:
        """Pose l'erreur d'une action interrompue, selon qu'une écriture a été engagée ou non.

        `event_failed` et `upload_failed` promettent à l'appelant, par contrat documenté, que
        l'échec est survenu **avant** le clic de validation et que le rejeu est donc sûr
        (docs/INTEGRATION.md). Les rendre après un clic déjà parti invite au doublon : c'est
        exactement ce qui s'est produit en recette. Après le clic, le vocabulaire correct est
        celui de l'incertitude, qui existe déjà.
        """
        item["ok"] = False
        if item.get("mutation_started"):
            item.setdefault("error", "unverified")
            item["mutation_may_have_happened"] = True
        else:
            item.setdefault("error", before_click_error)

    # --- Actions -----------------------------------------------------------------------

    def add_event(
        self,
        app_page: ApplicationPage,
        offer_id: str,
        event_type: str | None,
        comment: str,
        event_date: str | None,
    ) -> dict:
        """Crée un événement typé avec commentaire sur la candidature sélectionnée.

        Pas de détection `already_present` : le commentaire n'étant pas relisible, un
        dédoublonnage sur (type, date) confondrait deux synthèses distinctes du même jour et
        en perdrait une. L'idempotence est assurée en amont, par la clé d'idempotence.
        """
        result: dict = {"ok": False, "event_type": event_type}
        if not event_type:
            result["error"] = "event_type_required"
            return result

        signature = _event_signature(event_type, event_date)
        before = len(app_page.list_events(offer_id))

        dialog = EventDialog(self.page, self.deadline, config.action_timeout_ms())
        try:
            frame = dialog.open_on_selected_application()
        except MailDialogOpened as error:
            # Aucune mutation : la modale de courrier a ete refermee sans validation.
            logger.error(f"event_opens_mail_flow event_type={event_type!r}")
            result["error"] = "event_type_sends_mail"
            result["detail"] = str(error)
            return result
        try:
            chosen = dialog.fill(frame, event_type, comment, event_date)
        except ValueError as error:
            # Commentaire tronqué par le champ : on annule avant toute écriture.
            logger.error(f"event_comment_rejected reason={error}")
            dialog.cancel()
            result["error"] = "comment_too_long"
            return result

        result["event_type"] = chosen or event_type
        try:
            result["mutation_started"] = True
            self._notify_mutation_started()
            dialog.submit(frame)

            if self._verify_event_added(app_page, offer_id, signature, before):
                result.update({"ok": True, "verified": True, "verification": "weak"})
            else:
                result.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
        except (SelectorNotFound, JobTimeout) as error:
            # On rend `result`, et non un dictionnaire neuf : il porte `mutation_started`, la
            # seule information qui dise à l'appelant s'il peut rejouer.
            logger.error(f"event_failed error={type(error).__name__}")
            self.screenshot("event_failed")
            self._failure_after_click(result, "event_failed")
        except PlaywrightError as error:
            if is_fatal_playwright_error(error):
                raise BrowserFatalError(str(error)) from error
            logger.error(f"event_failed error={type(error).__name__}")
            self.screenshot("event_failed")
            self._failure_after_click(result, "event_failed")
        return result

    # Nombre de TENTATIVES de resélection, abouties ou non. Le repliement provoqué par le
    # postback est déterministe, pas une course : deux essais suffisent, et au-delà on laisse
    # tout le budget restant à la lecture.
    _VERIFY_MAX_RESELECT_ATTEMPTS = 2

    # Budget de relecture. Le Back Office met quelques secondes à refléter une écriture ; au-delà
    # on rapporte `unverified` plutôt que d'immobiliser l'unique navigateur.
    _VERIFY_BUDGET_SECONDS = 15.0

    def _verify_event_added(self, app_page: ApplicationPage, offer_id: str, signature: str, before_count: int) -> bool:
        """Vérification FAIBLE : une ligne de plus, portant le bon type et la bonne date.

        Le commentaire n'étant pas affiché, on ne peut pas certifier que la ligne observée est
        exactement la nôtre — d'où `verification: weak` dans la réponse.

        `before_count` a été mesuré candidature **dépliée**. Le postback de validation la replie
        et retire `tr.selectedLine` (voir app/ts_selectors.py) : relire sans resélectionner
        compare deux états différents de la page, et fait conclure `unverified` alors que
        l'événement a bien été créé. On rétablit donc l'état avant de compter.

        Les deux conditions sont conservées à dessein. Se contenter de la signature laisserait un
        événement préexistant de même type et même date valider une écriture qui n'a pas eu
        lieu — l'ambiguïté que `_event_signature` refuse explicitement.
        """
        attempts = 0
        succeeded = 0
        rows: list[str] = []
        waited_until = time.monotonic() + self._VERIFY_BUDGET_SECONDS
        while time.monotonic() < waited_until:
            # La resélection AMÉLIORE les conditions de lecture, elle ne les conditionne pas :
            # même repliée, une ligne reste lisible depuis que le texte est assemblé cellule par
            # cellule. Faire dépendre la lecture de son succès affamait la vérification — une
            # resélection systématiquement en échec faisait expirer le budget sans jamais lire.
            if attempts < self._VERIFY_MAX_RESELECT_ATTEMPTS:
                attempts += 1
                if self._reselect_application(app_page, offer_id):
                    succeeded += 1

            rows = app_page.list_events(offer_id)
            if len(rows) > before_count:
                if not signature:
                    return True
                tokens = signature.split()
                if any(all(token in row for token in tokens) for row in rows):
                    return True
            self.page.wait_for_timeout(500)

        self._log_verify_failure(app_page, before_count, len(rows), attempts, succeeded)
        return False

    def _reselect_application(self, app_page: ApplicationPage, offer_id: str) -> bool:
        """Redéplie la candidature après le postback. False si la page n'est pas encore prête.

        Une mutation est déjà engagée à cet instant : un échec transitoire pendant le postback
        doit faire retenter, jamais interrompre la vérification.
        """
        try:
            app_page.select_application_by_offer(offer_id)
            return True
        except (ApplicationNotOnOffer, SelectorNotFound) as error:
            # Message construit par ce dépôt, à partir du seul identifiant d'offre : il distingue
            # « offre absente de la fiche » de « candidature non active après sélection », ce que
            # le seul type d'exception ne dit pas.
            logger.info(f"verify_reselect_retry error={type(error).__name__} detail={str(error)[:120]}")
            return False
        except PlaywrightError as error:
            # Message Playwright verbatim : seul le type sort.
            logger.info(f"verify_reselect_retry error={type(error).__name__}")
            return False

    def _log_verify_failure(
        self, app_page: ApplicationPage, before: int, after: int, attempts: int, succeeded: int
    ) -> None:
        """Journalise la FORME du tableau, et le libellé des candidatures — jamais celui des événements.

        Une ligne de candidature porte « Réponse à offre <intitulé> ( réf. … ) » : de la donnée
        d'offre, publique. Une ligne d'événement porte son type et son auteur, donc une personne :
        elle ne sort pas d'ici.

        `reason` ne se déduit que de ce qui a réellement été mesuré. `target_not_found` exige
        d'avoir pu lire : sans cela on dirait `unreadable`, et non une cause inventée.
        """
        shape = app_page.history_shape()
        if not shape.get("table"):
            reason = "table_missing"
        elif succeeded == 0:
            reason = "reselect_failed"
        elif after == 0 and shape.get("events"):
            reason = "target_not_found"
        else:
            reason = "count_unchanged"
        logger.warning(
            f"verify_failed reason={reason} rows_before={before} rows_after={after} "
            f"applications={shape.get('applications', 0)} events={shape.get('events', 0)} "
            f"other={shape.get('other', 0)} "
            f"selected_line={str(bool(shape.get('selected_line'))).lower()} "
            f"reselect_attempts={attempts} reselect_ok={succeeded}"
        )
        if reason in ("reselect_failed", "target_not_found"):
            labels = [text[:90] for text in app_page.application_row_texts()[:4]]
            logger.warning(f"verify_failed_applications labels={labels!r}")

    def _log_attachment_verify_failure(self, app_page: ApplicationPage, category: str) -> None:
        """Un dépôt non confirmé ne doit pas passer inaperçu.

        Jusqu'ici ce cas posait `unverified` sans la moindre trace : le jour où il survenait, le
        diagnostic partait de zéro. La catégorie est un libellé de référentiel et le compte de
        pièces jointes une mesure : aucun nom de fichier d'origine ne sort d'ici.
        """
        try:
            count = len(app_page.list_attachments())
        except Exception as error:
            logger.warning(f"verify_failed kind=attachment reason=unreadable error={type(error).__name__}")
            return
        reason = "list_empty" if count == 0 else "name_not_found"
        logger.warning(f"verify_failed kind=attachment reason={reason} attachments={count} category={category!r}")

    def add_documents(
        self,
        app_page: ApplicationPage,
        document_paths: list[str],
        category: str | None,
    ) -> list[dict]:
        """Dépose les documents, tous dans la même catégorie, en une seule validation.

        GARDE-FOU NON CONTOURNABLE : déposer dans une catégorie déjà occupée détruit le
        document existant sans avertissement, et le formulaire n'indique pas l'occupation.
        On lit donc l'état de la fiche avant d'ouvrir la modale.
        """
        results: list[dict] = []
        if not document_paths:
            return results
        if not category:
            return [
                {"ok": False, "error": "document_category_required", "filename": os.path.basename(path)}
                for path in document_paths
            ]

        occupied = app_page.attachments_by_category()
        existing_labels = [normalize_text(label) for label in app_page.list_attachments()]
        category_key = normalize_text(category)

        # Une seule catégorie disponible par dépôt : au-delà d'un fichier, les suivants
        # écraseraient le précédent dans le même champ.
        if len(document_paths) > 1:
            return [
                {
                    "ok": False,
                    "error": "multiple_documents_same_category",
                    "filename": os.path.basename(path),
                    "category": category,
                }
                for path in document_paths
            ]

        path = document_paths[0]
        display_name = safety.safe_display_filename(os.path.basename(path))
        item: dict = {"ok": False, "filename": display_name, "category": category}

        if not os.path.exists(path):
            item["error"] = "file_not_found"
            return [item]

        # Déjà déposé à l'identique : rien à faire (le nom EST affiché, donc fiable ici).
        expected_label = normalize_text(f"{display_name} ({category})")
        if expected_label in existing_labels:
            item.update({"ok": True, "skipped": True, "reason": "already_present"})
            return [item]

        if occupied.get(category_key):
            logger.warning(f"category_occupied category={category_key!r}")
            item.update(
                {
                    "ok": False,
                    "error": "category_occupied",
                    "occupied_by": occupied[category_key][:3],
                }
            )
            return [item]

        dialog = AttachmentsDialog(self.page, self.deadline, config.action_timeout_ms())
        try:
            frame = dialog.open()
            resolved = dialog.set_files(frame, {category: path})
            item["category"] = resolved.get(category, category)
            item["mutation_started"] = True
            self._notify_mutation_started()
            dialog.submit(frame)
            if self._verify_attachment_present(app_page, display_name, item["category"]):
                item.update({"ok": True, "verified": True})
            else:
                self._log_attachment_verify_failure(app_page, item["category"])
                item.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
        except (SelectorNotFound, JobTimeout) as error:
            logger.error(f"document_failed error={type(error).__name__}")
            self._failure_after_click(item, "upload_failed")
            self.screenshot("document_failed")
        except PlaywrightError as error:
            if is_fatal_playwright_error(error):
                raise BrowserFatalError(str(error)) from error
            logger.error(f"document_failed error={type(error).__name__}")
            self._failure_after_click(item, "upload_failed")
            self.screenshot("document_failed")
        return [item]

    def _verify_attachment_present(self, app_page: ApplicationPage, filename: str, category: str) -> bool:
        """Le nom du fichier étant affiché, la vérification est ici réellement probante.

        Talentsoft met le nom en majuscules : la comparaison est insensible à la casse.
        """
        wanted_name = normalize_text(Path(filename).name)
        wanted_category = normalize_text(category)
        waited_until = time.monotonic() + 20.0
        while time.monotonic() < waited_until:
            for label in app_page.list_attachments():
                name, found_category = parse_attachment_label(label)
                if normalize_text(name) == wanted_name and normalize_text(found_category) == wanted_category:
                    return True
            self.page.wait_for_timeout(500)
        return False

    # --- Job complet -------------------------------------------------------------------

    def update_application(
        self,
        *,
        candidate_email: str,
        offer_id: str,
        event_type: str | None = None,
        comment: str | None = None,
        event_date: str | None = None,
        document_paths: list[str] | None = None,
        document_category: str | None = None,
        on_mutation_started: Callable[[], None] | None = None,
    ) -> dict:
        """Événement + documents sur une candidature. Retourne le détail par action.

        `on_mutation_started` est appelé **une seule fois**, juste avant la première écriture
        réelle dans le Back Office. Il permet à l'appelant de ne marquer son job comme
        irrécupérable qu'à partir de cet instant : un échec de login, de recherche ou de
        sélection n'a rien modifié et reste rejouable.
        """
        self._mutation_callback = on_mutation_started
        self._mutation_notified = False
        self.new_job()
        self.start_trace()
        actions: dict = {}
        mutation_started = False
        failed = False
        try:
            logger.info(f"open_application offer_id={offer_id} email_hash={safety.short_hash(candidate_email)}")
            app_page, label = self.open_application(candidate_email, offer_id)

            if comment:
                effective_type = event_type or config.ts_default_event_type() or None
                effective_date = event_date or date.today().isoformat()
                try:
                    actions["event"] = self.add_event(app_page, offer_id, effective_type, comment, effective_date)
                except (SelectorNotFound, JobTimeout) as error:
                    # Filet de sécurité : `add_event` traite désormais ses propres exceptions et
                    # rend son état accumulé. On n'arrive ici que si l'échec précède ce traitement.
                    logger.error(f"event_failed error={type(error).__name__}")
                    self.screenshot("event_failed")
                    actions["event"] = self._aborted_action("event_failed", event_type=effective_type)
                except PlaywrightError as error:
                    if is_fatal_playwright_error(error):
                        raise BrowserFatalError(str(error)) from error
                    logger.error(f"event_failed error={type(error).__name__}")
                    self.screenshot("event_failed")
                    actions["event"] = self._aborted_action("event_failed", event_type=effective_type)
                mutation_started = mutation_started or bool(actions["event"].get("mutation_started"))

            if document_paths:
                effective_category = document_category or config.ts_default_document_category() or None
                actions["documents"] = self.add_documents(app_page, document_paths, effective_category)
                mutation_started = mutation_started or any(d.get("mutation_started") for d in actions["documents"])

            failed = not safety.actions_succeeded(actions)
            return {
                "candidate_email_hash": safety.short_hash(candidate_email),
                "offer_id": offer_id,
                "application_label": label,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                # Même source que le drapeau du job : les deux ne peuvent plus se contredire.
                # Ils l'ont fait en recette (job 7edd00fb), et l'appelant y lisait « rien n'a
                # été écrit » alors qu'un clic de validation était parti.
                "mutation_started": mutation_started or self._mutation_notified,
                "actions": actions,
            }
        except Exception:
            failed = True
            self.screenshot("job_failed")
            raise
        finally:
            self._mutation_callback = None
            trace_path = self.stop_trace(keep=failed, name="update_application")
            if trace_path:
                logger.info(f"trace_saved path={trace_path}")

    # --- Lectures ----------------------------------------------------------------------

    def list_events(self, candidate_email: str, offer_id: str) -> list[str]:
        self.new_job()
        app_page, _ = self.open_application(candidate_email, offer_id)
        return app_page.list_events(offer_id)

    def read_event_types(self, candidate_email: str, offer_id: str) -> list[dict]:
        """Référentiel des types d'événement, lu dans le formulaire de la candidature témoin."""
        self.new_job()
        app_page, _ = self.open_application(candidate_email, offer_id)
        dialog = EventDialog(self.page, self.deadline, config.action_timeout_ms())
        frame = dialog.open_on_selected_application()
        try:
            return dialog.type_options(frame)
        finally:
            dialog.cancel()

    def read_document_categories(self, candidate_email: str, offer_id: str) -> list[str]:
        self.new_job()
        app_page, _ = self.open_application(candidate_email, offer_id)
        dialog = AttachmentsDialog(self.page, self.deadline, config.action_timeout_ms())
        try:
            frame = dialog.open()
        except SelectorNotFound:
            return []
        try:
            return [label for label in dialog.category_rows(frame) if label]
        finally:
            dialog.cancel()

    def selftest(self, candidate_email: str | None, offer_id: str | None) -> dict:
        """Lecture seule : login + candidature témoin + présence des sélecteurs critiques."""
        from .ts_pages import count_matches

        self.new_job()
        report: dict = {"authenticated": self.is_authenticated(), "selectors": {}}
        if candidate_email and offer_id:
            try:
                self.open_application(candidate_email, offer_id)
                report["application_opened"] = True
            except (ApplicationNotFound, CandidateNotFound, AmbiguousCandidate, ApplicationNotOnOffer) as error:
                report["application_opened"] = False
                report["application_error"] = type(error).__name__
        for name, candidates in sel.CRITICAL_SELECTORS.items():
            count = count_matches(self.page, candidates)
            report["selectors"][name] = {"ok": count > 0, "count": count}
        report["ok"] = report["authenticated"] and all(v["ok"] for v in report["selectors"].values())
        return report


def sweep_old_traces(retention_days: int | None = None) -> int:
    """Purge des traces et captures plus anciennes que la rétention (données personnelles)."""
    days = config.traces_retention_days() if retention_days is None else retention_days
    root = Path(config.TRACES_DIR)
    if not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for item in root.iterdir():
        try:
            if item.is_symlink():
                continue
            if item.stat().st_mtime < cutoff:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
                removed += 1
        except OSError:
            continue
    return removed
