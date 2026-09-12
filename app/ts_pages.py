"""Page objects du Back Office Talentsoft. Aucun sélecteur en dur : tout vient de ts_selectors."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable

from playwright.sync_api import Locator, Page

from . import ts_selectors as sel

logger = logging.getLogger(__name__)


class SelectorNotFound(Exception):
    """Aucun candidat n'a matché dans le délai imparti."""


class LoginError(Exception):
    """Connexion refusée (identifiants, compte bloqué, page inattendue). Jamais de secret dans le message."""


class Deadline:
    """Budget temps global d'un job, partagé par toutes les actions."""

    def __init__(self, seconds: float):
        self.expires_at = time.monotonic() + seconds

    def remaining_ms(self, cap_ms: int) -> int:
        remaining = int((self.expires_at - time.monotonic()) * 1000)
        if remaining <= 0:
            raise JobTimeout("job_timeout")
        return min(remaining, cap_ms)


class JobTimeout(Exception):
    """Le budget temps global du job est épuisé."""


def first_locator(
    page: Page,
    candidates: Iterable[str],
    timeout_ms: int,
    *,
    require_visible: bool = True,
    scope: Locator | None = None,
) -> Locator:
    """Premier candidat présent (et visible si demandé), en boucle jusqu'au délai."""
    candidates = list(candidates)
    root = scope if scope is not None else page
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        for candidate in candidates:
            try:
                locator = root.locator(candidate)
                if locator.count() == 0:
                    continue
                first = locator.first
                if not require_visible or first.is_visible():
                    return first
            except Exception as error:  # sélecteur invalide ou page en transition
                logger.debug(f"selector_probe_failed candidate={candidate!r} error={type(error).__name__}")
        if time.monotonic() >= deadline:
            raise SelectorNotFound(f"Aucun sélecteur trouvé parmi {len(candidates)} candidats: {candidates[0]!r} ...")
        page.wait_for_timeout(250)


def any_present(page: Page, candidates: Iterable[str], *, require_visible: bool = True) -> bool:
    for candidate in candidates:
        try:
            locator = page.locator(candidate)
            if locator.count() == 0:
                continue
            if not require_visible or locator.first.is_visible():
                return True
        except Exception:
            continue
    return False


def count_matches(page: Page, candidates: Iterable[str]) -> int:
    total = 0
    for candidate in candidates:
        try:
            total += page.locator(candidate).count()
        except Exception:
            continue
    return total


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def choose_option(page: Page, control: Locator, wanted: str, timeout_ms: int) -> str:
    """Sélectionne `wanted` (libellé ou valeur) dans un <select> natif ou un combobox custom."""
    tag = (control.evaluate("el => el.tagName") or "").lower()
    wanted_norm = normalize_text(wanted)

    if tag == "select":
        options = control.locator("option")
        labels = options.all_inner_texts()
        values = [options.nth(i).get_attribute("value") or "" for i in range(len(labels))]
        for label, value in zip(labels, values, strict=False):
            if normalize_text(label) == wanted_norm or normalize_text(value) == wanted_norm:
                control.select_option(value=value, timeout=timeout_ms)
                return label.strip()
        for label, value in zip(labels, values, strict=False):
            if wanted_norm and wanted_norm in normalize_text(label):
                control.select_option(value=value, timeout=timeout_ms)
                return label.strip()
        raise SelectorNotFound(f"Option introuvable dans le select: {wanted!r}")

    # Combobox custom : ouvrir puis cliquer l'option par texte.
    control.click(timeout=timeout_ms)
    option = page.locator(f"role=option[name=/^\\s*{re.escape(wanted)}\\s*$/i]").first
    if option.count() == 0:
        option = page.locator(f"role=option[name=/{re.escape(wanted)}/i]").first
    if option.count() == 0:
        option = page.locator(f"text=/^\\s*{re.escape(wanted)}\\s*$/i").first
    option.click(timeout=timeout_ms)
    return wanted


def list_options(control: Locator) -> list[str]:
    tag = (control.evaluate("el => el.tagName") or "").lower()
    if tag == "select":
        labels = control.locator("option").all_inner_texts()
        return [label.strip() for label in labels if label.strip()]
    return []


class LoginPage:
    def __init__(self, page: Page, base_url: str, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.base_url = base_url
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def is_displayed(self) -> bool:
        return any_present(self.page, sel.LOGIN_PAGE_MARKERS)

    def is_authenticated_view(self) -> bool:
        url = (self.page.url or "").lower()
        if any(fragment in url for fragment in sel.LOGIN_URL_FRAGMENTS):
            return False
        if self.is_displayed():
            return False
        return any_present(self.page, sel.AUTHENTICATED_MARKERS)

    def error_text(self) -> str:
        for candidate in sel.LOGIN_ERROR:
            try:
                locator = self.page.locator(candidate)
                if locator.count() and locator.first.is_visible():
                    return normalize_text(locator.first.inner_text())[:200]
            except Exception:
                continue
        return ""

    def submit_credentials(self, username: str, password: str) -> None:
        username_input = first_locator(self.page, sel.LOGIN_USERNAME, self._t())
        username_input.fill(username, timeout=self._t())
        password_input = first_locator(self.page, sel.LOGIN_PASSWORD, self._t())
        password_input.fill(password, timeout=self._t())
        # Vérification sans jamais journaliser la valeur : longueur saisie = longueur attendue.
        typed_length = len(password_input.input_value(timeout=self._t()))
        if typed_length != len(password):
            raise LoginError("password_fill_mismatch")
        submit = first_locator(self.page, sel.LOGIN_SUBMIT, self._t())
        submit.click(timeout=self._t())


class ApplicationPage:
    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def is_ready(self) -> bool:
        return any_present(self.page, sel.APPLICATION_READY)

    def is_not_found(self) -> bool:
        return any_present(self.page, sel.APPLICATION_NOT_FOUND)

    def wait_ready(self) -> None:
        first_locator(self.page, sel.APPLICATION_READY, self._t())

    def open_events_tab(self) -> None:
        tab = first_locator(self.page, sel.EVENTS_TAB, self._t())
        tab.click(timeout=self._t())

    def list_events(self) -> list[str]:
        try:
            first_locator(self.page, sel.EVENT_ROWS, min(self._t(), 5000), require_visible=False)
        except SelectorNotFound:
            return []
        texts = _all_texts(self.page, sel.EVENT_ROWS)
        return [normalize_text(t) for t in texts if t and t.strip()]

    def open_attachments_tab(self) -> None:
        tab = first_locator(self.page, sel.ATTACHMENTS_TAB, self._t())
        tab.click(timeout=self._t())

    def list_attachments(self) -> list[str]:
        texts = _all_texts(self.page, sel.ATTACHMENT_ROWS)
        return [normalize_text(t) for t in texts if t and t.strip()]


def _all_texts(page: Page, candidates: Iterable[str]) -> list[str]:
    for candidate in candidates:
        try:
            locator = page.locator(candidate)
            if locator.count() > 0:
                return locator.all_inner_texts()
        except Exception:
            continue
    return []


class EventDialog:
    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def open(self) -> Locator:
        button = first_locator(self.page, sel.ADD_EVENT_BUTTON, self._t())
        button.click(timeout=self._t())
        return first_locator(self.page, sel.EVENT_DIALOG, self._t())

    def type_options(self, dialog: Locator) -> list[str]:
        try:
            control = first_locator(self.page, sel.EVENT_TYPE_SELECT, min(self._t(), 5000), scope=dialog)
        except SelectorNotFound:
            return []
        return list_options(control)

    def fill(self, dialog: Locator, event_type: str | None, comment: str, event_date: str | None) -> str | None:
        chosen_type = None
        if event_type:
            control = first_locator(self.page, sel.EVENT_TYPE_SELECT, self._t(), scope=dialog)
            chosen_type = choose_option(self.page, control, event_type, self._t())
        comment_input = first_locator(self.page, sel.EVENT_COMMENT, self._t(), scope=dialog)
        comment_input.fill(comment, timeout=self._t())
        if event_date:
            try:
                date_input = first_locator(self.page, sel.EVENT_DATE, min(self._t(), 3000), scope=dialog)
                date_input.fill(event_date, timeout=self._t())
            except SelectorNotFound:
                logger.info("event_date ignorée : aucun champ date dans la boîte de dialogue")
        return chosen_type

    def submit(self, dialog: Locator) -> None:
        submit = first_locator(self.page, sel.EVENT_SUBMIT, self._t(), scope=dialog)
        submit.click(timeout=self._t())
        try:
            dialog.wait_for(state="hidden", timeout=self._t())
        except Exception:
            logger.info("La boîte de dialogue événement n'a pas disparu dans le délai")

    def close(self) -> None:
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass


class AttachmentsPanel:
    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def open_form(self) -> None:
        # Le formulaire est soit directement affiché, soit ouvert par un bouton "Ajouter".
        if any_present(self.page, sel.ADD_ATTACHMENT_BUTTON):
            button = first_locator(self.page, sel.ADD_ATTACHMENT_BUTTON, self._t())
            button.click(timeout=self._t())
            return
        if any_present(self.page, sel.ATTACHMENT_FILE_INPUT, require_visible=False):
            return
        raise SelectorNotFound("Ni bouton d'ajout ni champ fichier pour les pièces jointes")

    def category_options(self) -> list[str]:
        try:
            control = first_locator(self.page, sel.ATTACHMENT_CATEGORY_SELECT, min(self._t(), 5000))
        except SelectorNotFound:
            return []
        return list_options(control)

    def add(self, path: str, category: str | None, display_name: str) -> str | None:
        file_input = first_locator(self.page, sel.ATTACHMENT_FILE_INPUT, self._t(), require_visible=False)
        file_input.set_input_files(path, timeout=self._t())
        # Les autres champs sont cherchés dans le formulaire du champ fichier quand il existe :
        # évite de cliquer un bouton homonyme ailleurs dans la page ("Ajouter ...").
        form = file_input.locator("xpath=ancestor::form[1]")
        scope = form if form.count() > 0 else None
        chosen_category = None
        if category:
            control = first_locator(self.page, sel.ATTACHMENT_CATEGORY_SELECT, self._t(), scope=scope)
            chosen_category = choose_option(self.page, control, category, self._t())
        try:
            name_input = first_locator(self.page, sel.ATTACHMENT_NAME_INPUT, min(self._t(), 3000), scope=scope)
            name_input.fill(display_name, timeout=self._t())
        except SelectorNotFound:
            pass
        try:
            submit = first_locator(self.page, sel.ATTACHMENT_SUBMIT, min(self._t(), 5000), scope=scope)
            submit.click(timeout=self._t())
        except SelectorNotFound:
            logger.info("Pas de bouton de validation : upload considéré immédiat")
        return chosen_category
