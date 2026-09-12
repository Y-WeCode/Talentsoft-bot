"""TalentsoftBot : pilote le Back Office Talentsoft avec Playwright (API sync).

Toutes les méthodes s'exécutent dans l'unique thread de l'executor navigateur.
Règles :
- aucune valeur de mot de passe, de cookie ou de commentaire dans les logs ;
- chaque mutation est relue dans le DOM (verified) ;
- un échec après début de mutation n'est jamais rejoué ici ni par l'appelant.
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
    ApplicationPage,
    AttachmentsPanel,
    Deadline,
    EventDialog,
    JobTimeout,
    LoginError,
    LoginPage,
    SelectorNotFound,
    normalize_text,
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


def _event_fingerprint(comment: str) -> str:
    return normalize_text(comment)[:200]


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
        # Navigation principale limitée au tenant : les cookies de session ne sortent jamais du domaine.
        self.context.route("**/*", self._guard_route)
        self.page = self.context.new_page()

    def _guard_route(self, route, request) -> None:
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            if not safety.is_same_origin(request.url):
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
        login_page = self._login_page()
        if login_page.is_authenticated_view():
            # Session restaurée depuis storage_state.
            self._authenticated = True
            self.save_storage_state()
            return
        self.login()

    def login(self) -> None:
        username = config.ts_username()
        password = config.ts_password()
        if not username or not password:
            raise LoginError("missing_credentials")
        login_page = self._login_page()
        if not login_page.is_displayed():
            self._goto(self.base_url + sel.LOGIN_PATH)
            login_page = self._login_page()
        if not login_page.is_displayed():
            if login_page.is_authenticated_view():
                self._authenticated = True
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

    # --- Fiche candidature -------------------------------------------------------------

    def open_application(self, application_url: str) -> ApplicationPage:
        """Ouvre la fiche. Session expirée : re-login une fois (aucune mutation encore)."""
        safety.validate_talentsoft_url(application_url)
        for attempt in (1, 2):
            self._goto(application_url)
            login_page = self._login_page()
            if login_page.is_displayed():
                if attempt == 2:
                    raise SessionExpired("login_redirect_twice")
                logger.info("session_expired_relogin")
                self._authenticated = False
                self.login()
                continue
            break
        app_page = ApplicationPage(self.page, self.deadline, config.action_timeout_ms())
        if app_page.is_not_found():
            raise ApplicationNotFound(application_url)
        try:
            app_page.wait_ready()
        except SelectorNotFound as error:
            if app_page.is_not_found():
                raise ApplicationNotFound(application_url) from error
            raise
        return app_page

    # --- Actions -----------------------------------------------------------------------

    def add_event(
        self, app_page: ApplicationPage, event_type: str | None, comment: str, event_date: str | None
    ) -> dict:
        fingerprint = _event_fingerprint(comment)
        result: dict = {"ok": False, "event_type": event_type}
        try:
            app_page.open_events_tab()
        except SelectorNotFound:
            logger.info("events_tab_not_found : la fiche expose peut-être l'historique directement")

        existing = app_page.list_events()
        if fingerprint and any(fingerprint in row for row in existing):
            logger.info("event_skip_already_present")
            return {"ok": True, "skipped": True, "reason": "already_present", "event_type": event_type}

        dialog_helper = EventDialog(self.page, self.deadline, config.action_timeout_ms())
        dialog = dialog_helper.open()
        chosen = dialog_helper.fill(dialog, event_type, comment, event_date)
        result["event_type"] = chosen or event_type
        result["mutation_started"] = True
        dialog_helper.submit(dialog)

        verified = self._verify_event_present(app_page, fingerprint)
        if verified:
            result.update({"ok": True, "verified": True})
        else:
            result.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
        return result

    def _verify_event_present(self, app_page: ApplicationPage, fingerprint: str) -> bool:
        waited_until = time.monotonic() + 10.0
        while time.monotonic() < waited_until:
            rows = app_page.list_events()
            if fingerprint and any(fingerprint in row for row in rows):
                return True
            self.page.wait_for_timeout(500)
        return False

    def add_documents(self, app_page: ApplicationPage, document_paths: list[str], category: str | None) -> list[dict]:
        results: list[dict] = []
        if not document_paths:
            return results
        try:
            app_page.open_attachments_tab()
        except SelectorNotFound:
            logger.info("attachments_tab_not_found : la fiche expose peut-être les pièces jointes directement")

        panel = AttachmentsPanel(self.page, self.deadline, config.action_timeout_ms())
        for path in document_paths:
            display_name = safety.safe_display_filename(os.path.basename(path))
            stem = normalize_text(Path(display_name).stem)
            item: dict = {"ok": False, "filename": display_name, "category": category}
            if not os.path.exists(path):
                item["error"] = "file_not_found"
                results.append(item)
                continue
            try:
                existing = app_page.list_attachments()
                if stem and any(stem in row for row in existing):
                    item.update({"ok": True, "skipped": True, "reason": "already_present"})
                    results.append(item)
                    continue
                panel.open_form()
                item["mutation_started"] = True
                chosen = panel.add(path, category, display_name)
                if chosen:
                    item["category"] = chosen
                if self._verify_attachment_present(app_page, stem):
                    item.update({"ok": True, "verified": True})
                else:
                    item.update({"ok": False, "error": "unverified", "mutation_may_have_happened": True})
            except (SelectorNotFound, JobTimeout) as error:
                logger.error(f"document_failed error={type(error).__name__}")
                item.update({"ok": False, "error": "upload_failed"})
                self.screenshot("document_failed")
            except PlaywrightError as error:
                if is_fatal_playwright_error(error):
                    raise BrowserFatalError(str(error)) from error
                logger.error(f"document_failed error={type(error).__name__}")
                item.update({"ok": False, "error": "upload_failed"})
                self.screenshot("document_failed")
            results.append(item)
        return results

    def _verify_attachment_present(self, app_page: ApplicationPage, stem: str) -> bool:
        waited_until = time.monotonic() + 15.0
        while time.monotonic() < waited_until:
            rows = app_page.list_attachments()
            if stem and any(stem in row for row in rows):
                return True
            self.page.wait_for_timeout(500)
        return False

    # --- Job complet -------------------------------------------------------------------

    def update_application(
        self,
        *,
        application_id: str,
        application_url: str,
        event_type: str | None = None,
        comment: str | None = None,
        event_date: str | None = None,
        document_paths: list[str] | None = None,
        document_category: str | None = None,
    ) -> dict:
        """Événement + documents sur une fiche. Retourne le détail par action."""
        self.new_job()
        self.start_trace()
        actions: dict = {}
        mutation_started = False
        failed = False
        try:
            logger.info(f"open_application application_id={application_id}")
            app_page = self.open_application(application_url)

            if comment:
                effective_type = event_type or config.ts_default_event_type() or None
                effective_date = event_date or date.today().isoformat()
                try:
                    actions["event"] = self.add_event(app_page, effective_type, comment, effective_date)
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
                "application_id": application_id,
                "application_url": application_url,
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

    def list_events(self, application_url: str) -> list[str]:
        self.new_job()
        app_page = self.open_application(application_url)
        try:
            app_page.open_events_tab()
        except SelectorNotFound:
            pass
        return app_page.list_events()

    def read_event_types(self, application_url: str) -> list[str]:
        self.new_job()
        app_page = self.open_application(application_url)
        try:
            app_page.open_events_tab()
        except SelectorNotFound:
            pass
        dialog_helper = EventDialog(self.page, self.deadline, config.action_timeout_ms())
        dialog = dialog_helper.open()
        try:
            return dialog_helper.type_options(dialog)
        finally:
            dialog_helper.close()

    def read_document_categories(self, application_url: str) -> list[str]:
        self.new_job()
        app_page = self.open_application(application_url)
        try:
            app_page.open_attachments_tab()
        except SelectorNotFound:
            pass
        panel = AttachmentsPanel(self.page, self.deadline, config.action_timeout_ms())
        try:
            panel.open_form()
        except SelectorNotFound:
            return []
        try:
            return panel.category_options()
        finally:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

    def selftest(self, application_url: str | None) -> dict:
        """Lecture seule : login + fiche témoin + présence des sélecteurs critiques."""
        from .ts_pages import count_matches

        self.new_job()
        report: dict = {"authenticated": self.is_authenticated(), "selectors": {}}
        if application_url:
            try:
                self.open_application(application_url)
                report["application_opened"] = True
            except ApplicationNotFound:
                report["application_opened"] = False
        for name, candidates in sel.CRITICAL_SELECTORS.items():
            if name in ("add_event_button", "events_tab") and application_url:
                try:
                    ApplicationPage(self.page, self.deadline, config.action_timeout_ms()).open_events_tab()
                except SelectorNotFound:
                    pass
            if name in ("add_attachment_button", "attachments_tab") and application_url:
                try:
                    ApplicationPage(self.page, self.deadline, config.action_timeout_ms()).open_attachments_tab()
                except SelectorNotFound:
                    pass
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
