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
- **Le commentaire d'un événement n'est pas relisible** : la vérification reste faible et
  le dédoublonnage par relecture est impossible (il produirait des faux positifs).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
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
    SelectorNotFound,
    normalize_text,
    parse_attachment_label,
    to_talentsoft_date,
)

logger = logging.getLogger(__name__)

_FATAL_FRAGMENTS = (
    "target closed",
    "has been closed",
    "browser closed",
    "connection closed",
    "target page, context or browser has been closed",
    "protocol error",
    "browser process exited",
)


class BrowserFatalError(Exception):
    """Le navigateur ou le contexte est perdu : la session doit être recyclée."""


class ApplicationNotFound(Exception):
    """La fiche candidature n'existe pas ou n'est pas accessible au compte technique."""


class SessionExpired(Exception):
    """Talentsoft a renvoyé vers la page de login en cours de job."""


def is_fatal_playwright_error(error: BaseException) -> bool:
    message = str(error).lower()
    return isinstance(error, PlaywrightError) and any(fragment in message for fragment in _FATAL_FRAGMENTS)


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

        context_kwargs = {"accept_downloads": False, "locale": "fr-FR"}
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
        """Le bandeau Didomi recouvre la page et intercepte les clics : à traiter une fois."""
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
            # Session restaurée depuis storage_state.
            self._authenticated = True
            self.save_storage_state()
            return
        self.login()

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
        login_page.submit_credentials(username, password)
        try:
            self.page.wait_for_load_state(
                "networkidle", timeout=self.deadline.remaining_ms(config.navigation_timeout_ms())
            )
        except Exception:
            pass

        # Attente d'un état stable : session ouverte ou erreur affichée.
        waited_until = time.monotonic() + config.navigation_timeout_ms() / 1000.0
        while time.monotonic() < waited_until:
            self._dismiss_cookies()
            if login_page.is_authenticated_view():
                self._authenticated = True
                self.save_storage_state()
                logger.info("login_success")
                return
            error_text = login_page.error_text()
            if error_text:
                logger.warning("login_rejected")
                raise LoginError("credentials_rejected")
            self.page.wait_for_timeout(500)
        raise LoginError("login_not_confirmed")

    def _wait_for_login_form(self, login_page: LoginPage) -> None:
        waited_until = time.monotonic() + min(config.navigation_timeout_ms(), 30000) / 1000.0
        while time.monotonic() < waited_until:
            if login_page.is_displayed() or login_page.is_authenticated_view():
                return
            self.page.wait_for_timeout(300)

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
            if login_page.is_displayed() or login_page.is_account_choice_displayed():
                if attempt == 2:
                    raise SessionExpired("login_redirect_twice")
                logger.info("session_expired_relogin")
                self._authenticated = False
                self.login()
                continue
            break

        search = GlobalSearch(self.page, self.deadline, config.action_timeout_ms())
        search.search(candidate_email)
        search.open_single_result()

        app_page = ApplicationPage(self.page, self.deadline, config.action_timeout_ms())
        if app_page.is_not_found():
            raise ApplicationNotFound(f"candidat introuvable pour l'email fourni (offre {offer_id})")
        app_page.wait_ready()
        app_page.open_events_tab()
        label = app_page.select_application_by_offer(offer_id)
        logger.info(f"application_opened offer_id={offer_id}")
        return app_page, label

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
        frame = dialog.open_from_workflow_action(event_type)
        try:
            chosen = dialog.fill(frame, event_type, comment, event_date)
        except ValueError as error:
            # Commentaire tronqué par le champ : on annule avant toute écriture.
            logger.error(f"event_comment_rejected reason={error}")
            dialog.cancel()
            result["error"] = "comment_too_long"
            return result

        result["event_type"] = chosen or event_type
        result["mutation_started"] = True
        dialog.submit(frame)

        if self._verify_event_added(app_page, offer_id, signature, before):
            result.update({"ok": True, "verified": True, "verification": "weak"})
        else:
            result.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
        return result

    def _verify_event_added(self, app_page: ApplicationPage, offer_id: str, signature: str, before_count: int) -> bool:
        """Vérification FAIBLE : une ligne de plus, portant le bon type et la bonne date.

        Le commentaire n'étant pas affiché, on ne peut pas certifier que la ligne observée est
        exactement la nôtre — d'où `verification: weak` dans la réponse.
        """
        waited_until = time.monotonic() + 15.0
        while time.monotonic() < waited_until:
            rows = app_page.list_events(offer_id)
            if len(rows) > before_count:
                if not signature:
                    return True
                tokens = signature.split()
                if any(all(token in row for token in tokens) for row in rows):
                    return True
            self.page.wait_for_timeout(500)
        return False

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
            dialog.submit(frame)
            if self._verify_attachment_present(app_page, display_name, item["category"]):
                item.update({"ok": True, "verified": True})
            else:
                item.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
        except (SelectorNotFound, JobTimeout) as error:
            logger.error(f"document_failed error={type(error).__name__}")
            item.setdefault("error", "upload_failed")
            item["ok"] = False
            self.screenshot("document_failed")
        except PlaywrightError as error:
            if is_fatal_playwright_error(error):
                raise BrowserFatalError(str(error)) from error
            logger.error(f"document_failed error={type(error).__name__}")
            item.setdefault("error", "upload_failed")
            item["ok"] = False
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
    ) -> dict:
        """Événement + documents sur une candidature. Retourne le détail par action."""
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
                    logger.error(f"event_failed error={type(error).__name__}")
                    self.screenshot("event_failed")
                    actions["event"] = {"ok": False, "error": "event_failed", "event_type": effective_type}
                except PlaywrightError as error:
                    if is_fatal_playwright_error(error):
                        raise BrowserFatalError(str(error)) from error
                    logger.error(f"event_failed error={type(error).__name__}")
                    self.screenshot("event_failed")
                    actions["event"] = {"ok": False, "error": "event_failed", "event_type": effective_type}
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
                "mutation_started": mutation_started,
                "actions": actions,
            }
        except Exception:
            failed = True
            self.screenshot("job_failed")
            raise
        finally:
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
        links = self.page.locator(sel.WORKFLOW_ACTION_LINKS[0])
        if links.count() == 0:
            return []
        first_action = links.first.inner_text(timeout=5000)
        frame = dialog.open_from_workflow_action(first_action)
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
