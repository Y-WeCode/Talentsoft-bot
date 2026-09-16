"""Page objects du Back Office Talentsoft. Aucun sélecteur en dur : tout vient de ts_selectors.

Particularités du Back Office, relevées en phase 0 (docs/DISCOVERY.md) :

- Les deux formulaires de mutation (événement, pièces jointes) vivent dans des **iframes**
  (RadWindow Telerik). D'où `frame_locator` et non `page.locator`.
- La navigation interne se fait par **postback** ASP.NET : après un clic, l'URL ne change pas
  et il n'y a pas d'événement de navigation. On attend un changement observable du DOM.
- Certaines actions déclenchent un **`confirm()` natif**, que Playwright rejette par défaut.
  Le handler est posé dans `scraper.TalentsoftBot`.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime

from playwright.sync_api import FrameLocator, Locator, Page

from . import safety
from . import ts_selectors as sel

logger = logging.getLogger(__name__)


class SelectorNotFound(Exception):
    """Aucun candidat n'a matché dans le délai imparti."""


class LoginError(Exception):
    """Connexion refusée (identifiants, compte bloqué, page inattendue). Jamais de secret dans le message."""


class AmbiguousCandidate(Exception):
    """La recherche par email ne désigne pas un candidat unique : on refuse d'agir au hasard."""


class CandidateNotFound(Exception):
    """Aucun candidat pour cet email."""


class ApplicationNotOnOffer(Exception):
    """Le candidat existe mais n'a pas de candidature sur l'offre visée."""


class CategoryOccupied(Exception):
    """La catégorie de pièce jointe contient déjà un document : déposer l'écraserait."""


class MailDialogOpened(Exception):
    """L'action ouvre un envoi de courrier au candidat, pas un formulaire d'événement.

    Le bot referme la modale et refuse de poursuivre : valider y enverrait un message réel.
    """


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
    scope: Locator | FrameLocator | None = None,
) -> Locator:
    """Premier candidat présent (et visible si demandé), en boucle jusqu'au délai.

    `scope` accepte un Locator ou un FrameLocator : les deux exposent `.locator()`.
    """
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


def any_present(
    page: Page,
    candidates: Iterable[str],
    *,
    require_visible: bool = True,
    scope: Locator | FrameLocator | None = None,
) -> bool:
    root = scope if scope is not None else page
    for candidate in candidates:
        try:
            locator = root.locator(candidate)
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


def to_talentsoft_date(value: str | None) -> str | None:
    """`YYYY-MM-DD` (contrat HTTP) vers `JJ/MM/AAAA` (champ du Back Office).

    Une valeur déjà au format Talentsoft est renvoyée telle quelle.
    """
    if not value:
        return None
    value = value.strip()
    if re.match(r"^\d{2}/\d{2}/\d{4}$", value):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError as error:
        raise ValueError("event_date doit être au format YYYY-MM-DD") from error


def parse_attachment_label(label: str) -> tuple[str, str]:
    """« NOM_FICHIER.PDF (Catégorie) » vers (nom, catégorie).

    Talentsoft affiche la catégorie entre parenthèses en fin de libellé, et met le nom du
    fichier en majuscules. La catégorie peut elle-même contenir des parenthèses
    (« Profil(s) individuel(s) ») : on prend la DERNIÈRE parenthèse ouvrante de premier niveau.
    """
    label = (label or "").strip()
    match = re.match(r"^(.*?)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$", label)
    if not match:
        return label, ""
    return match.group(1).strip(), match.group(2).strip()


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


class CookieBanner:
    """Bandeau Didomi, ancré en bas de la fenêtre avec un z-index maximal.

    Il ne recouvre pas toute la page, mais masque la bande basse : un contrôle situé là
    devient incliquable. On le refuse une fois, au premier chargement.
    """

    def __init__(self, page: Page, action_timeout_ms: int):
        self.page = page
        self.action_timeout_ms = action_timeout_ms

    def is_displayed(self) -> bool:
        return any_present(self.page, sel.COOKIE_BANNER)

    def refuse(self) -> bool:
        """Refuse les finalités non essentielles. True si un bandeau a été traité."""
        if not self.is_displayed():
            return False
        try:
            button = first_locator(self.page, sel.COOKIE_REFUSE, min(self.action_timeout_ms, 5000))
            button.click(timeout=self.action_timeout_ms)
            self.page.wait_for_timeout(500)
            logger.info("cookie_banner_refused")
            return True
        except (SelectorNotFound, JobTimeout):
            logger.warning("cookie_banner_present_but_no_refuse_button")
            return False


class LoginPage:
    """Parcours d'authentification fédérée : choix du compte, puis identifiant / mot de passe."""

    def __init__(self, page: Page, base_url: str, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.base_url = base_url
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def is_displayed(self) -> bool:
        return any_present(self.page, sel.LOGIN_PAGE_MARKERS)

    def is_account_choice_displayed(self) -> bool:
        return any_present(self.page, sel.ACCOUNT_CHOICE_MARKERS)

    def is_authenticated_view(self) -> bool:
        url = (self.page.url or "").lower()
        if any(fragment in url for fragment in sel.LOGIN_URL_FRAGMENTS):
            return False
        if self.is_displayed() or self.is_account_choice_displayed():
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

    def choose_account(self, wanted: str) -> str:
        """Sélectionne un compte sur l'écran de fédération et poursuit.

        `wanted` vide : on ne choisit que s'il n'y a qu'une seule option, sinon on refuse —
        se tromper de compte donnerait des droits ou un périmètre inattendus.

        Le bouton radio est souvent **masqué en CSS**, seul son libellé étant visible et
        cliquable. `check()` échouerait alors sur un élément non actionnable : on clique donc
        le libellé, comme le ferait un recruteur, avec repli sur un cochage forcé.
        """
        radios = self.page.locator(sel.ACCOUNT_CHOICE_RADIOS[0])
        count = radios.count()
        if count == 0:
            raise LoginError("account_choice_no_option")

        # Tout est relevé en UNE évaluation : interroger les options une par une multiplierait
        # les allers-retours, et chacun peut expirer si la page bouge entre-temps.
        options = self.page.evaluate(
            """() => Array.from(document.querySelectorAll("input[type='radio']")).map(r => ({
                id: r.id || '',
                value: (r.value || '').trim(),
                label: ((r.closest('label,a,div') || r).innerText || '').trim(),
            }))"""
        )
        labels = [option["label"] for option in options]
        ids = [option["id"] for option in options]
        # La `value` du radio est un identifiant ASCII (`airfrance.fr`), bien plus sûr à
        # transporter dans un .env que le libellé affiché, qui est accentué.
        values = [(option["value"] or option["id"]).strip() for option in options]

        # Libellés et identifiants de compte applicatif, pas des données personnelles :
        # les journaliser est ce qui rend un échec de choix diagnosticable.
        logger.info(
            f"account_choice_options count={count} "
            f"options={[(v[:30], normalize_text(x)[:40]) for v, x in zip(values, labels, strict=False)]}"
        )

        target = -1
        if wanted:
            wanted_norm = normalize_text(wanted)
            # Identifiant exact d'abord : sans ambiguïté et insensible aux libellés accentués.
            for index, value in enumerate(values):
                if value and normalize_text(value) == wanted_norm:
                    target = index
                    break
            if target < 0:
                for index, label in enumerate(labels):
                    if wanted_norm == normalize_text(label):
                        target = index
                        break
            if target < 0:
                for index, label in enumerate(labels):
                    if wanted_norm and wanted_norm in normalize_text(label):
                        target = index
                        break
            if target < 0:
                raise LoginError(
                    f"account_choice_not_found: TS_ACCOUNT_CHOICE={wanted_norm[:40]!r} ne correspond "
                    f"a aucune des {count} options (ni identifiant, ni libelle)"
                )
        elif count == 1:
            target = 0
        else:
            raise LoginError(f"account_choice_ambiguous: {count} options, TS_ACCOUNT_CHOICE non renseigné")

        how = self._select_account_option(ids[target], values[target])
        logger.info(f"account_choice_selected_via={how}")

        submit = first_locator(self.page, sel.ACCOUNT_CHOICE_SUBMIT, self._t())
        submit.click(timeout=self._t())
        chosen = normalize_text(labels[target])[:80]
        logger.info(f"account_choice_submitted choice={chosen[:40]!r}")
        return chosen

    def _select_account_option(self, radio_id: str, radio_value: str) -> str:
        """Coche l'option de compte, en opérant DANS la page.

        Les gestes Playwright ne conviennent pas ici, et l'essai en production l'a montré :

        - le radio porte `visibility: hidden` : ni `check()` ni `click()` ne l'atteignent ;
        - son libellé est enveloppé dans un `<a href="#">` qui **intercepte** le clic, si bien
          que cliquer `label[for]` ne coche rien ;
        - ce clic fait néanmoins **bouger la page**, ce qui périme le locator : toute
          vérification ultérieure via ce locator expire sur le timeout par défaut (30 s), et
          trois gestes enchaînés coûtaient 90 s pour finir en échec.

        On fait donc tout en une seule évaluation : retrouver l'option par son identifiant ou
        sa valeur, la cocher, notifier la page, et rendre compte de l'état obtenu. Un seul
        aller-retour, aucun locator à périmer.

        Retourne le geste qui a abouti, pour le journal.
        """
        script = """([wantedId, wantedValue]) => {
            const radios = Array.from(document.querySelectorAll("input[type='radio']"));
            const radio = radios.find(r => (wantedId && r.id === wantedId))
                       || radios.find(r => (wantedValue && r.value === wantedValue));
            if (!radio) return {ok: false, how: 'introuvable', count: radios.length};

            // `click()` appele dans la page coche ET laisse s'executer les gestionnaires,
            // contrairement a un evenement synthetique, qui ne declenche pas le comportement
            // par defaut du navigateur.
            try { radio.click(); } catch (e) { /* poursuivre avec le repli */ }
            if (radio.checked) return {ok: true, how: 'click'};

            // Repli : forcer l'etat, puis prevenir la page comme l'aurait fait un vrai clic.
            radio.checked = true;
            radio.dispatchEvent(new Event('input', {bubbles: true}));
            radio.dispatchEvent(new Event('change', {bubbles: true}));
            return {ok: radio.checked, how: radio.checked ? 'checked' : 'sans_effet'};
        }"""
        try:
            result = self.page.evaluate(script, [radio_id, radio_value])
        except Exception as error:
            raise LoginError(
                f"account_choice_not_selectable: evaluation impossible ({type(error).__name__})"
            ) from error

        if not result.get("ok"):
            raise LoginError(
                f"account_choice_not_selectable: {result.get('how')} (options vues: {result.get('count')})"
            )
        return str(result.get("how"))

    def submit_credentials(self, username: str, password: str) -> None:
        """Renseigne le formulaire de l'IdP et le soumet, en vérifiant que la soumission part.

        Constaté en production : cliquer le bouton « Connexion » remplissait le formulaire sans
        rien envoyer — la capture d'écran de l'échec montrait les deux champs encore saisis,
        alors qu'un POST rejeté par ASP.NET réaffiche la page avec le mot de passe vidé.

        On tente donc plusieurs gestes, du plus proche de l'utilisateur au plus direct, et on
        vérifie à chaque fois que la page a réellement quitté le formulaire.
        """
        username_input = first_locator(self.page, sel.LOGIN_USERNAME, self._t())
        username_input.fill(username, timeout=self._t())
        password_input = first_locator(self.page, sel.LOGIN_PASSWORD, self._t())
        password_input.fill(password, timeout=self._t())
        # Vérification sans jamais journaliser la valeur : longueur saisie = longueur attendue.
        typed_length = len(password_input.input_value(timeout=self._t()))
        if typed_length != len(password):
            raise LoginError("password_fill_mismatch")

        attempts = [
            ("bouton", self._submit_by_button),
            # Entrée dans le champ mot de passe : le geste naturel, et il emprunte la
            # soumission native du navigateur plutôt que le gestionnaire du bouton.
            ("entree", lambda: password_input.press("Enter", timeout=min(self._t(), 5000))),
            # Dernier recours : demander au formulaire de se soumettre lui-même.
            # `requestSubmit()` déclenche la validation et l'événement `submit`, contrairement
            # à `submit()` qui les court-circuiterait.
            ("request_submit", self._submit_by_form),
        ]

        journal: list[str] = []
        for name, action in attempts:
            try:
                action()
            except Exception as error:
                journal.append(f"{name}:{type(error).__name__}")
                continue
            if self._submission_left_the_form():
                logger.info(f"login_submitted_via={name}")
                return
            journal.append(f"{name}:sans_effet")

        raise LoginError(f"login_form_not_submitted: aucun geste n'a envoyé le formulaire [{', '.join(journal)}]")

    def _submit_by_button(self) -> None:
        submit = first_locator(self.page, sel.LOGIN_SUBMIT, min(self._t(), 5000))
        submit.click(timeout=min(self._t(), 5000))

    def _submit_by_form(self) -> None:
        submitted = self.page.evaluate(
            """() => {
                const field = document.querySelector("input[type='password']");
                const form = field && field.form;
                if (!form) return false;
                if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
                form.submit();
                return true;
            }"""
        )
        if not submitted:
            raise SelectorNotFound("aucun formulaire ne porte le champ mot de passe")

    def _submission_left_the_form(self) -> bool:
        """La page a-t-elle quitté le formulaire de connexion ?

        Un formulaire toujours affiché avec ses champs renseignés signale une soumission qui
        n'est jamais partie ; un POST rejeté, lui, revient avec le mot de passe vidé.
        """
        # 4 s suffisent a constater une navigation ; au-dela on paierait ce delai a chaque
        # geste infructueux, et a chaque connexion du bot.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            try:
                if not self.is_displayed():
                    return True
                password = self.page.locator(sel.LOGIN_PASSWORD[0]).first
                if password.count() and not password.input_value(timeout=2000):
                    # Formulaire réaffiché mais vidé : la soumission est bien partie, et c'est
                    # le serveur qui l'a rejetée. L'appelant le constatera via le message d'erreur.
                    return True
            except Exception:
                return True  # la page a changé sous nos pieds : la soumission est partie
            self.page.wait_for_timeout(400)
        return False


class GlobalSearch:
    """Recherche globale du Back Office. Seul chemin praticable vers une fiche candidat.

    Le Back Office n'accepte pas les identifiants de l'API Recruiting Customer (docs/DISCOVERY.md) :
    on retrouve le candidat par son email, qui s'est révélé discriminant.
    """

    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def search(self, term: str) -> None:
        field = first_locator(self.page, sel.GLOBAL_SEARCH_INPUT, self._t())
        field.click(timeout=self._t())
        field.fill("", timeout=self._t())
        field.fill(term, timeout=self._t())
        field.press("Enter", timeout=self._t())

    def result_items(self) -> list[Locator]:
        """Suggestions de la recherche, débarrassées de tout ce qui n'en est pas une.

        Le tenant monte son menu utilisateur avec les mêmes rôles ARIA que des suggestions :
        « Changer de mot de passe », « Centre d'aide », **« Déconnexion »**. Cliquer cette
        dernière en croyant ouvrir une fiche ferait perdre la session à chaque tentative.
        Le filtre est donc appliqué ici, et pas seulement dans le sélecteur.
        """
        for candidate in sel.SEARCH_RESULT_ITEMS:
            try:
                locator = self.page.locator(candidate)
                count = locator.count()
                if not count:
                    continue
                items = []
                for index in range(count):
                    item = locator.nth(index)
                    if self._is_excluded(item):
                        continue
                    items.append(item)
                if items:
                    return items
            except Exception:
                continue
        return []

    def _is_excluded(self, item: Locator) -> bool:
        try:
            label = normalize_text(item.inner_text(timeout=2000))
        except Exception:
            return False
        if not label:
            return False
        for forbidden in sel.SEARCH_RESULT_EXCLUDED_LABELS:
            if forbidden in label:
                logger.warning(f"search_result_ignored label={label[:40]!r} : entrée de menu, pas un candidat")
                return True
        return False

    def open_single_result(self, expected_email: str = "") -> bool:
        """Ouvre l'unique résultat. False si la fiche s'est ouverte directement.

        Lève `AmbiguousCandidate` si plusieurs résultats : ne jamais deviner, écrire sur le
        dossier d'un autre candidat serait une divulgation de données personnelles.

        Quand `expected_email` est fourni, la suggestion est confrontée à cet email avant
        d'être ouverte. Le tenant affiche l'adresse dans le libellé
        (« PAOLI Georges(Ref: 584408)bailleulg@gmail.com ») : cette vérification transforme un
        clic de confiance en clic vérifié, pour un coût nul.
        """
        deadline = time.monotonic() + min(self._t(), 15000) / 1000.0
        while time.monotonic() < deadline:
            if any_present(self.page, sel.APPLICATION_READY):
                return False
            items = self.result_items()
            if len(items) == 1:
                self._assert_matches_email(items[0], expected_email)
                items[0].click(timeout=self._t())
                return True
            if len(items) > 1:
                raise AmbiguousCandidate(f"{len(items)} résultats")
            self.page.wait_for_timeout(300)
        raise CandidateNotFound("aucun résultat de recherche")

    def _assert_matches_email(self, item: Locator, expected_email: str) -> None:
        """Refuse une suggestion qui porte une adresse différente de celle demandée.

        Le libellé n'affiche pas toujours l'email : son absence n'est donc pas un motif de
        refus. En revanche, une adresse **présente et différente** signale qu'on s'apprête à
        ouvrir le dossier de quelqu'un d'autre.
        """
        if not expected_email:
            return
        try:
            label = normalize_text(item.inner_text(timeout=3000))
        except Exception:
            return
        wanted = normalize_text(expected_email)
        if wanted in label:
            return
        found = re.findall(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", label)
        if found and not any(normalize_text(address) == wanted for address in found):
            raise AmbiguousCandidate(
                f"la suggestion porte une autre adresse que celle demandée "
                f"({safety.short_hash(found[0])} vs {safety.short_hash(expected_email)})"
            )


class ApplicationPage:
    """Fiche candidat, onglet Historique : sélection d'une candidature et lecture de son état."""

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
        self._wait_postback(sel.APPLICATIONS_HISTORY_TABLE)

    def _wait_postback(self, expected: Iterable[str], *, require_visible: bool = False) -> None:
        """Attend la fin d'un postback : l'URL ne change pas, seul le DOM bouge.

         compte : le Back Office laisse dans le DOM des lignes repliees en
        , dont la presence ne prouve aucun changement d'etat.
        """
        deadline = time.monotonic() + min(self._t(), 20000) / 1000.0
        while time.monotonic() < deadline:
            if any_present(self.page, expected, require_visible=require_visible):
                return
            self.page.wait_for_timeout(250)
        raise SelectorNotFound("postback sans effet observable")

    # --- Sélection de la candidature ----------------------------------------------------

    def offer_links(self) -> list[tuple[str, Locator]]:
        """(texte, locator) de chaque lien de candidature. Le texte porte la référence de l'offre."""
        out: list[tuple[str, Locator]] = []
        locator = self.page.locator(sel.APPLICATION_OFFER_LINK[0])
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                out.append((item.inner_text(timeout=2000), item))
            except Exception:
                continue
        return out

    def select_application_by_offer(self, offer_id: str) -> str:
        """Ouvre la candidature correspondant à l'offre, puis vérifie qu'elle est bien active.

        Lève `ApplicationNotOnOffer` si le candidat n'a pas postulé à cette offre.
        """
        matches = [(text, link) for text, link in self.offer_links() if safety.offer_reference_matches(text, offer_id)]
        if not matches:
            raise ApplicationNotOnOffer(f"offre {offer_id} absente de la fiche")
        if len(matches) > 1:
            # Deux candidatures du même candidat sur la même offre : cas anormal, on refuse.
            raise ApplicationNotOnOffer(f"{len(matches)} candidatures pour l'offre {offer_id}")

        text, link = matches[0]
        link.click(timeout=self._t())
        self.page.wait_for_timeout(500)
        # Le postback de sélection déplie les événements ET charge les actions de workflow du
        # panneau Outils : avant lui, ni les uns ni les autres ne sont exploitables. On attend
        # donc des lignes d'événement VISIBLES — leur simple présence dans le DOM ne prouve
        # rien, elles y sont déjà en `display: none` quand la candidature est repliée.
        try:
            self._wait_postback(sel.EVENT_ROWS, require_visible=True)
        except SelectorNotFound:
            # Une candidature sans aucun événement est légitime : rien à déplier.
            logger.info("aucune ligne d'événement visible après sélection de la candidature")
        if not self.selected_offer_matches(offer_id):
            raise ApplicationNotOnOffer(f"candidature {offer_id} non active après sélection")
        return normalize_text(text)[:120]

    def selected_offer_matches(self, offer_id: str) -> bool:
        """Garde-fou avant toute mutation : la candidature active est-elle la bonne ?

        Deux preuves, de la plus directe à la plus structurelle :

        1. `tr.selectedLine` porte la référence de l'offre. Le Back Office pose cette classe
           sur la candidature sélectionnée — elle est absente au chargement de la fiche et
           après un postback de mutation, mais présente après une sélection explicite, qui est
           toujours ce que fait le bot.
        2. À défaut, l'ordre du DOM : les lignes d'événement suivent la ligne de leur
           candidature, jusqu'à la candidature suivante.
        """
        selected = self._selected_row_text()
        if selected is not None:
            return safety.offer_reference_matches(selected, offer_id)

        rows = self._history_rows()
        if not rows:
            return False
        current_is_target = False
        saw_target = False
        for kind, text in rows:
            if kind == "application":
                current_is_target = safety.offer_reference_matches(text, offer_id)
                saw_target = saw_target or current_is_target
            elif kind == "event" and not current_is_target:
                # Des événements sont rattachés à une AUTRE candidature : mauvaise cible.
                return False
        return saw_target

    def _selected_row_text(self) -> str | None:
        """Texte de la ligne marquée sélectionnée, ou None si le Back Office n'en marque aucune."""
        try:
            locator = self.page.locator(sel.SELECTED_APPLICATION_ROW[0])
            if locator.count() != 1:
                return None
            return locator.first.inner_text(timeout=3000)
        except Exception:
            return None

    def _history_rows(self) -> list[tuple[str, str]]:
        """Lignes du tableau d'historique, dans l'ordre du DOM : ('application'|'event', texte).

        Le texte est assemblé **cellule par cellule**, et non lu via `innerText`. Celui-ci rend
        un résultat différent selon que la ligne est affichée ou non : la spec HTML le fait
        retomber sur `textContent` pour un élément non rendu, si bien qu'une ligne visible rend
        « Type<TAB>Date » et la même ligne repliée « TypeDate ». Or les lignes d'événement sont
        repliées dès qu'aucune candidature n'est sélectionnée — et après un postback de mutation.
        Tout ce qui s'appuie sur ce texte (référence d'offre, signature d'événement) deviendrait
        alors dépendant du rendu.
        """
        try:
            return self.page.evaluate(
                """(selectors) => {
                    const table = selectors.map(s => document.querySelector(s)).find(Boolean);
                    if (!table) return [];
                    return Array.from(table.rows).map(r => {
                        const cls = r.className || '';
                        const kind = cls.includes('trChildrenEvent') ? 'event'
                            : cls.includes('ch_content_outerrep') || cls.includes('selectedLine') ? 'application'
                            : 'other';
                        const text = Array.from(r.cells)
                            .map(c => (c.textContent || '').replace(/\\s+/g, ' ').trim())
                            .filter(Boolean)
                            .join(' ');
                        return [kind, text];
                    }).filter(r => r[0] !== 'other');
                }""",
                list(sel.APPLICATIONS_HISTORY_TABLE),
            )
        except Exception as error:
            logger.debug(f"history_rows_failed error={type(error).__name__}")
            return []

    def history_shape(self) -> dict:
        """Forme du tableau d'historique — des compteurs, **jamais** de texte de ligne.

        Sert à diagnostiquer un échec de vérification sans ouvrir de trace : les compteurs
        suffisent à distinguer « les lignes ont disparu » de « la candidature cible n'est plus
        reconnue ». Le texte des lignes porte des données personnelles et n'a rien à faire
        dans un log.
        """
        empty = {"table": False, "applications": 0, "events": 0, "other": 0, "selected_line": False}
        try:
            return self.page.evaluate(
                """(selectors) => {
                    const table = selectors.map(s => document.querySelector(s)).find(Boolean);
                    if (!table) {
                        return {table: false, applications: 0, events: 0, other: 0, selected_line: false};
                    }
                    let applications = 0, events = 0, other = 0;
                    for (const r of Array.from(table.rows)) {
                        const cls = r.className || '';
                        if (cls.includes('trChildrenEvent')) events++;
                        else if (cls.includes('ch_content_outerrep') || cls.includes('selectedLine')) applications++;
                        else other++;
                    }
                    return {
                        table: true,
                        applications: applications,
                        events: events,
                        other: other,
                        selected_line: !!table.querySelector('tr.selectedLine'),
                    };
                }""",
                list(sel.APPLICATIONS_HISTORY_TABLE),
            )
        except Exception as error:
            logger.debug(f"history_shape_failed error={type(error).__name__}")
            return empty

    def list_events(self, offer_id: str | None = None) -> list[str]:
        """Événements de la candidature développée (ou de toutes si `offer_id` est absent)."""
        rows = self._history_rows()
        if offer_id is None:
            return [normalize_text(text) for kind, text in rows if kind == "event" and text.strip()]
        out: list[str] = []
        current_is_target = False
        for kind, text in rows:
            if kind == "application":
                current_is_target = safety.offer_reference_matches(text, offer_id)
            elif kind == "event" and current_is_target and text.strip():
                out.append(normalize_text(text))
        return out

    # --- Pièces jointes -----------------------------------------------------------------

    def list_attachments(self) -> list[str]:
        """Libellés bruts « NOM (Catégorie) » des pièces jointes de la fiche."""
        for candidate in sel.ATTACHMENT_ROWS:
            try:
                locator = self.page.locator(candidate)
                if locator.count() > 0:
                    return [text.strip() for text in locator.all_inner_texts() if text.strip()]
            except Exception:
                continue
        return []

    def attachments_by_category(self) -> dict[str, list[str]]:
        """Catégorie normalisée vers noms de fichiers présents.

        Sert le garde-fou anti-écrasement : déposer dans une catégorie occupée DÉTRUIT le
        document existant, et le formulaire de dépôt n'indique pas l'occupation.
        """
        out: dict[str, list[str]] = {}
        for label in self.list_attachments():
            name, category = parse_attachment_label(label)
            if not category:
                continue
            out.setdefault(normalize_text(category), []).append(name)
        return out


class EventDialog:
    """Formulaire « Création d'un événement », servi dans un iframe (RadWindow Telerik)."""

    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def frame(self) -> FrameLocator:
        return self.page.frame_locator(sel.EVENT_DIALOG_FRAME[0])

    def wait_open(self) -> FrameLocator:
        """Attend que l'iframe soit présente ET son formulaire chargé.

        Surveille en parallèle l'ouverture d'un parcours d'envoi de courrier : certaines
        actions de workflow ouvrent celui-ci au lieu du formulaire d'événement. Dans ce cas on
        referme et on abandonne, plutôt que d'attendre en laissant la modale ouverte.
        """
        deadline = time.monotonic() + min(self._t(), 20000) / 1000.0
        while time.monotonic() < deadline:
            self._abort_if_mail_dialog()
            if any_present(self.page, sel.EVENT_DIALOG_FRAME, require_visible=False):
                frame = self.frame()
                try:
                    if frame.locator(sel.EVENT_TYPE_SELECT[0]).count() > 0:
                        return frame
                except Exception:
                    pass
            self.page.wait_for_timeout(250)
        raise SelectorNotFound("formulaire d'événement non chargé")

    def _abort_if_mail_dialog(self) -> None:
        """Referme un parcours d'envoi de courrier ouvert par mégarde, et refuse de continuer.

        Constaté sur le tenant : l'action « Candidature à l'étude » n'ouvre pas le formulaire
        d'événement mais `ActionMailLanguageChoicePage`, dont le bouton « Valider » (`btnSend`)
        **envoie un courrier au candidat**. Poursuivre dans cette modale enverrait un message
        réel : on annule et on remonte une erreur explicite.
        """
        if not any_present(self.page, sel.FORBIDDEN_DIALOG_FRAMES, require_visible=False):
            return
        logger.error("mail_dialog_detected : parcours d'envoi de courrier ouvert, annulation")
        try:
            frame = self.page.frame_locator(sel.FORBIDDEN_DIALOG_FRAMES[0])
            cancel = first_locator(self.page, sel.FORBIDDEN_DIALOG_CANCEL, 5000, scope=frame, require_visible=False)
            cancel.click(timeout=5000)
            self.page.wait_for_timeout(1000)
        except Exception as error:
            logger.warning(f"mail_dialog_cancel_failed error={type(error).__name__}")
        raise MailDialogOpened("cette action ouvre un envoi de courrier au candidat, pas un formulaire d'événement")

    def open_on_selected_application(self) -> FrameLocator:
        """Ouvre « Création d'un événement » depuis la ligne de la candidature sélectionnée.

        Le bouton « Effectuer une action sur la candidature » (`btnEventActionNew`), porté par
        la ligne elle-même, est le SEUL chemin qui ouvre un formulaire de saisie complet —
        type, date et commentaire — et qui crée l'événement en une seule passe.

        Les actions du panneau Outils ne conviennent pas : selon le paramétrage, elles créent
        l'événement **sans proposer de commentaire**, ou ouvrent un **envoi de courrier** au
        candidat. Vérifié sur le tenant (docs/DISCOVERY.md).

        Ce bouton appartenant à la ligne de la candidature, la cible est sans ambiguïté :
        aucun risque d'écrire sur une autre candidature du même candidat.
        """
        row = self.page.locator(sel.SELECTED_APPLICATION_ROW[0])
        if row.count() != 1:
            raise SelectorNotFound(
                "aucune candidature sélectionnée : le bouton d'action appartient à sa ligne "
                "(sélectionner la candidature avant d'ouvrir le formulaire)"
            )
        button = first_locator(self.page, sel.EVENT_ACTION_BUTTON, self._t(), scope=row.first)
        button.click(timeout=self._t())
        return self.wait_open()

    def type_options(self, frame: FrameLocator | None = None) -> list[dict]:
        """Référentiel des types : [{'code': ..., 'label': ...}]."""
        frame = frame or self.frame()
        try:
            return frame.locator(sel.EVENT_TYPE_SELECT[0]).evaluate(
                "el => Array.from(el.options).map(o => ({code: o.value, label: (o.text||'').trim()}))"
                ".filter(o => o.label)"
            )
        except Exception as error:
            logger.debug(f"type_options_failed error={type(error).__name__}")
            return []

    def fill(self, frame: FrameLocator, event_type: str | None, comment: str, event_date: str | None) -> str | None:
        chosen_type = None
        if event_type:
            control = first_locator(self.page, sel.EVENT_TYPE_SELECT, self._t(), scope=frame, require_visible=False)
            chosen_type = choose_option(self.page, control, event_type, self._t())

        if event_date:
            ts_date = to_talentsoft_date(event_date)
            try:
                date_input = first_locator(self.page, sel.EVENT_DATE, min(self._t(), 5000), scope=frame)
                date_input.fill(ts_date, timeout=self._t())
            except SelectorNotFound:
                logger.info("event_date ignorée : aucun champ date dans le formulaire")

        comment_input = first_locator(self.page, sel.EVENT_COMMENT, self._t(), scope=frame)
        comment_input.fill(comment, timeout=self._t())
        # Le champ porte maxlength=2000 : on vérifie que rien n'a été tronqué.
        typed = comment_input.input_value(timeout=self._t())
        if len(typed) != len(comment):
            raise ValueError(f"commentaire tronqué par le Back Office ({len(typed)}/{len(comment)} caractères)")
        return chosen_type

    def submit(self, frame: FrameLocator) -> None:
        submit = first_locator(self.page, sel.EVENT_SUBMIT, self._t(), scope=frame, require_visible=False)
        submit.click(timeout=self._t())
        self._wait_closed()

    def cancel(self) -> None:
        try:
            frame = self.frame()
            button = first_locator(self.page, sel.EVENT_CANCEL, 3000, scope=frame, require_visible=False)
            button.click(timeout=3000)
            self._wait_closed()
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

    def _wait_closed(self) -> None:
        deadline = time.monotonic() + min(self._t(), 20000) / 1000.0
        while time.monotonic() < deadline:
            if not any_present(self.page, sel.EVENT_DIALOG_FRAME, require_visible=False):
                return
            self.page.wait_for_timeout(250)
        logger.info("la modale d'événement n'a pas disparu dans le délai")


class AttachmentsDialog:
    """Formulaire de dépôt de pièces jointes, servi dans un iframe.

    Une ligne par catégorie, chacune avec son propre champ fichier : la catégorie se choisit
    en remplissant le bon champ. Plusieurs catégories peuvent être servies en une validation.
    """

    def __init__(self, page: Page, deadline: Deadline, action_timeout_ms: int):
        self.page = page
        self.deadline = deadline
        self.action_timeout_ms = action_timeout_ms

    def _t(self) -> int:
        return self.deadline.remaining_ms(self.action_timeout_ms)

    def frame(self) -> FrameLocator:
        return self.page.frame_locator(sel.ATTACHMENT_DIALOG_FRAME[0])

    def open(self) -> FrameLocator:
        button = first_locator(self.page, sel.ADD_ATTACHMENT_BUTTON, self._t())
        button.click(timeout=self._t())
        return self.wait_open()

    def wait_open(self) -> FrameLocator:
        deadline = time.monotonic() + min(self._t(), 20000) / 1000.0
        while time.monotonic() < deadline:
            if any_present(self.page, sel.ATTACHMENT_DIALOG_FRAME, require_visible=False):
                frame = self.frame()
                try:
                    if frame.locator(sel.ATTACHMENT_FILE_INPUTS[0]).count() > 0:
                        return frame
                except Exception:
                    pass
            self.page.wait_for_timeout(250)
        raise SelectorNotFound("formulaire de pièces jointes non chargé")

    def category_rows(self, frame: FrameLocator | None = None) -> list[str]:
        """Libellés des catégories, dans l'ordre des lignes du formulaire."""
        frame = frame or self.frame()
        try:
            return frame.locator(sel.ATTACHMENT_FILE_INPUTS[0]).evaluate_all(
                """els => els.map(i => {
                    const tr = i.closest('tr');
                    if (!tr || !tr.cells.length) return '';
                    return (tr.cells[0].innerText || '').replace(/\\s+/g, ' ').trim();
                })"""
            )
        except Exception as error:
            logger.debug(f"category_rows_failed error={type(error).__name__}")
            return []

    def file_input_for(self, frame: FrameLocator, category: str) -> Locator:
        """Champ fichier de la ligne portant ce libellé de catégorie.

        Recherche par libellé et jamais par indice `ctlNN` : les indices se décalent dès
        qu'une catégorie est ajoutée au paramétrage du client.
        """
        labels = self.category_rows(frame)
        wanted = normalize_text(category)
        index = next((i for i, label in enumerate(labels) if normalize_text(label) == wanted), -1)
        if index < 0:
            index = next((i for i, label in enumerate(labels) if wanted and wanted in normalize_text(label)), -1)
        if index < 0:
            raise SelectorNotFound(f"catégorie de pièce jointe introuvable: {category!r}")
        return frame.locator(sel.ATTACHMENT_FILE_INPUTS[0]).nth(index)

    def set_files(self, frame: FrameLocator, files_by_category: dict[str, str]) -> dict[str, str]:
        """Remplit un champ par catégorie. Retourne le libellé réellement retenu par catégorie."""
        labels = self.category_rows(frame)
        resolved: dict[str, str] = {}
        for category, path in files_by_category.items():
            control = self.file_input_for(frame, category)
            control.set_input_files(path, timeout=self._t())
            wanted = normalize_text(category)
            match = next((label for label in labels if normalize_text(label) == wanted), None)
            resolved[category] = match or category
        return resolved

    def submit(self, frame: FrameLocator) -> None:
        submit = first_locator(self.page, sel.ATTACHMENT_SUBMIT, self._t(), scope=frame, require_visible=False)
        submit.click(timeout=self._t())
        self._wait_closed()

    def cancel(self) -> None:
        try:
            frame = self.frame()
            button = first_locator(self.page, sel.ATTACHMENT_CANCEL, 3000, scope=frame, require_visible=False)
            button.click(timeout=3000)
            self._wait_closed()
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

    def _wait_closed(self) -> None:
        deadline = time.monotonic() + min(self._t(), 30000) / 1000.0
        while time.monotonic() < deadline:
            if not any_present(self.page, sel.ATTACHMENT_DIALOG_FRAME, require_visible=False):
                return
            self.page.wait_for_timeout(250)
        logger.info("la modale de pièces jointes n'a pas disparu dans le délai")
