"""Bout en bout contre un faux Back Office servi par interception réseau Playwright.

Les fixtures reproduisent la structure réelle relevée en phase 0 (docs/DISCOVERY.md) :
authentification fédérée en deux écrans, onglets Telerik, historique à deux niveaux,
formulaires servis dans des iframes, `confirm()` natif, et surtout le comportement
d'écrasement d'une pièce jointe déposée dans une catégorie occupée.

Valide le code navigateur réel (launch, storage_state, route guard, frame_locator, fill,
select, set_input_files) sans dépendre du tenant. Sauté si aucun Chromium n'est disponible.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "fake_backoffice"
BASE = "https://fake.talent-soft.com"
IDP = "https://idp-fake.talent-soft.com"
# Espace collaborateur : le SSO y atterrit avant que le bot rejoigne le Back Office.
LANDING = "https://landing-fake.talent-soft.com"


def _chromium_available() -> bool:
    if os.getenv("BROWSER_EXECUTABLE_PATH"):
        return os.path.exists(os.environ["BROWSER_EXECUTABLE_PATH"])
    for candidate in ("/opt/pw-browsers/chromium", shutil.which("chromium"), shutil.which("chromium-browser")):
        if candidate and os.path.exists(candidate):
            os.environ["BROWSER_EXECUTABLE_PATH"] = candidate
            return True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium indisponible")


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeServer:
    """Sert le faux Back Office et son parcours d'authentification fédérée."""

    def __init__(self):
        self.requests: list[str] = []
        self.reset_session = False
        self.account_chosen = False
        self.credentials_posted = False
        self.landed = False
        self.expire_to_landing = False
        self.back_office_opened = False
        # Cookies restaures mais perimes : le tenant sert une page que le bot ne reconnait pas.
        # Consommee une seule fois — une fois le contexte vide, le parcours normal reprend.
        self.stale_restored_session = False
        # Reproduit un postback qui efface la premiere saisie du commentaire.
        self.wipe_first_comment = False
        self.blocked: list[str] = []

    def install(self, context):
        context.route("**/*", self.handle)

    def handle(self, route, request):
        url = request.url
        self.requests.append(url)
        # La session est portée par l'état du serveur et non par un cookie : le parcours
        # traverse plusieurs domaines, et un cookie posé sur l'hôte d'atterrissage ne vaudrait
        # pas pour le Back Office. C'est le serveur d'identité qui fait foi, comme en réel.
        logged_in = self.credentials_posted and not self.reset_session

        # Parcours fédéré, tel qu'observé : BASE -> choix du compte -> IdP -> retour BASE.
        if url.startswith(IDP):
            body = request.post_data or ""
            if "Username=" in body:
                # Identifiants postés : l'IdP renvoie vers le tenant, qui ouvrira la session.
                self.credentials_posted = True
                return route.fulfill(status=200, content_type="text/html", body=_page("sso-return.html"))
            if "account=" in body:
                self.account_chosen = True
            return route.fulfill(status=200, content_type="text/html", body=_page("login.html"))

        if url.startswith(LANDING + "/RedirectBackOffice.ashx"):
            # Point d'entree du Back Office : c'est lui qui ouvre la session applicative.
            self.back_office_opened = True
            return route.fulfill(status=200, content_type="text/html", body=_page("bo-entry.html"))

        if url.startswith(LANDING):
            # Atterrissage post-SSO, hors Back Office : le bot doit le traverser sans conclure
            # a un echec, puis naviguer de lui-meme vers TS_BASE_URL.
            self.landed = True
            return route.fulfill(status=200, content_type="text/html", body=_page("my-talentsoft.html"))

        if not url.startswith(BASE):
            self.blocked.append(url)
            return route.abort("blockedbyclient")

        path = url[len(BASE) :].split("?")[0]
        query = url[len(BASE) :].split("?")[1] if "?" in url[len(BASE) :] else ""

        if "authenticated=1" in query:
            return route.fulfill(
                status=200,
                content_type="text/html",
                body=_page("home.html"),
                headers={"Set-Cookie": "ts_session=1; path=/"},
            )

        if self.stale_restored_session:
            self.stale_restored_session = False
            return route.fulfill(
                status=200,
                content_type="text/html",
                body="<!doctype html><html lang='fr'><body><p>Session invalide</p></body></html>",
            )

        if self.expire_to_landing:
            # Session Back Office expiree : ce tenant ne montre PAS de formulaire de login, il
            # REDIRIGE vers l'espace collaborateur, sur un autre hote. Servir simplement son
            # contenu sous l'URL du Back Office ne reproduirait pas le cas : c'est le changement
            # d'origine qui trahit la perte de session.
            return route.fulfill(status=200, content_type="text/html", body=_page("sso-return.html"))

        if not logged_in:
            return route.fulfill(status=200, content_type="text/html", body=_page("account-choice.html"))

        if not self.back_office_opened:
            return route.fulfill(status=200, content_type="text/html", body=_page("sso-return.html"))

        if path == "/Pages/Applicants/MainPage.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("applicant.html"))
        if path == "/Pages/Applicants.Events/JobApplicationChildEventEdit.aspx":
            body = _page("event-dialog.html")
            if self.wipe_first_comment:
                body = body.replace("location.search", "'?wipefirst'")
            return route.fulfill(status=200, content_type="text/html", body=body)
        if path == "/Pages/Correspondence/ActionMailLanguageChoicePage.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("mail-language-dialog.html"))
        if path == "/Pages/Utils/AttachedFileEdit.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("attachment-dialog.html"))
        return route.fulfill(status=200, content_type="text/html", body=_page("home.html"))


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_BASE_URL", BASE)
    monkeypatch.setenv("TS_AUTH_HOSTS", "idp-fake.talent-soft.com,landing-fake.talent-soft.com")
    monkeypatch.setenv("TS_USERNAME", "bot@example.com")
    monkeypatch.setenv("TS_PASSWORD", "secret")
    monkeypatch.setenv("TS_ACCOUNT_CHOICE", "Accès principal")
    monkeypatch.setenv("HEADLESS_MODE", "true")
    monkeypatch.setenv("ACTION_TIMEOUT_MS", "5000")
    monkeypatch.setenv("NAVIGATION_TIMEOUT_MS", "8000")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "60")
    return tmp_path


@pytest.fixture
def bot(bot_env):
    from app.scraper import TalentsoftBot

    instance = TalentsoftBot()
    server = FakeServer()
    server.install(instance.context)
    instance._server = server
    instance.ensure_logged_in()
    yield instance
    instance.close()


def _pdf(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% faux pdf de test\n")
    return str(path)


# --- Authentification -------------------------------------------------------------------


def test_federated_login_traverses_account_choice_and_idp(bot):
    """Le parcours a quatre hotes doit aboutir, sans quoi le bot ne peut rien faire."""
    assert bot._server.account_chosen is True
    assert any(url.startswith(IDP) for url in bot._server.requests)
    assert bot.is_authenticated()


def test_login_survives_landing_outside_the_back_office(bot):
    """Le SSO atterrit sur l espace collaborateur, pas sur le Back Office.

    Les marqueurs du Back Office n y matchent pas : conclure a un echec a cet instant ferait
    rater un login reussi. Le bot doit traverser l atterrissage puis rejoindre TS_BASE_URL.
    """
    assert bot._server.landed is True, "l atterrissage hors Back Office n a pas eu lieu"
    assert bot.is_authenticated()
    # Et il termine bien sur le Back Office, pas sur l espace collaborateur.
    assert bot.page.url.startswith(BASE)


def test_navigation_outside_tenant_and_auth_hosts_is_blocked(bot):
    """Les hôtes d'authentification sont tolérés, tout le reste reste interdit."""
    from app import safety

    assert safety.is_allowed_navigation(f"{BASE}/Pages/Applicants/MainPage.aspx")
    assert safety.is_allowed_navigation(f"{IDP}/wsfed/issue")
    assert not safety.is_allowed_navigation("https://evil.example.com/")
    assert not safety.is_allowed_navigation("http://fake.talent-soft.com/")


# --- Ouverture d'une candidature ---------------------------------------------------------


def test_opens_application_by_email_and_offer(bot):
    _page_obj, label = bot.open_application("candidat@example.com", "25152")
    assert "25152" in label


def test_refuses_offer_the_candidate_did_not_apply_to(bot):
    from app.ts_pages import ApplicationNotOnOffer

    with pytest.raises(ApplicationNotOnOffer):
        bot.open_application("candidat@example.com", "99999")


def test_selects_the_right_application_among_several(bot):
    """Le candidat a deux candidatures : les événements lus doivent être ceux de la bonne."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    assert app_page.selected_offer_matches("25152")
    assert not app_page.selected_offer_matches("23770")


# --- Événements ---------------------------------------------------------------------------


def test_event_is_created_through_iframe_and_verified(bot):
    """Couvre le confirm() natif, l'iframe, le format de date et la vérification."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = len(app_page.list_events("25152"))
    result = bot.add_event(app_page, "25152", "Candidature à l'étude", "Synthèse Hippolyte.ai", "2026-09-14")
    assert result["ok"] is True, result
    assert result["verified"] is True
    assert result["verification"] == "weak"
    rows = app_page.list_events("25152")
    assert len(rows) == before + 1
    # La date a bien été convertie en JJ/MM/AAAA.
    assert any("14/09/2026" in row for row in rows)


def test_event_lands_on_the_selected_application_only(bot):
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    bot.add_event(app_page, "25152", "En attente", "Pour la bonne offre", "2026-09-14")
    assert app_page.list_events("23770") == []


def test_fake_back_office_folds_the_list_after_a_mutation_postback(bot):
    """Garde sur le fixture lui-meme.

    Le vrai Back Office replie l historique et retire `tr.selectedLine` apres un postback de
    mutation. Tant que le faux ne le reproduisait pas, il etait plus complaisant que le vrai et
    aucun test ne pouvait attraper une relecture faite dans le mauvais etat de page.
    """
    bot.open_application("candidat@example.com", "25152")
    assert bot.page.locator("tr.selectedLine").count() == 1
    assert bot.page.locator("tr.trChildrenEvent").count() > 0

    bot.page.evaluate("() => window.__addEvent('Candidature reactivee', '14/09/2026', 'x')")
    bot.page.wait_for_timeout(300)

    assert bot.page.locator("tr.selectedLine").count() == 0, "la marque de selection doit disparaitre"
    assert bot.page.locator("tr.trChildrenEvent").count() == 0, "la liste doit se replier"


def test_event_is_verified_despite_the_fold_caused_by_its_own_postback(bot):
    """La regression observee en recette : evenement bien cree, mais rendu `unverified`.

    Le compte de reference est pris candidature DEPLIEE ; le postback de validation la replie.
    Relire sans reselectionner compare deux etats differents de la page.
    """
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = len(app_page.list_events("25152"))

    result = bot.add_event(app_page, "25152", "En attente", "Synthese Hippolyte.ai", "2026-09-14")

    assert result["ok"] is True, result
    assert result["verified"] is True
    assert len(app_page.list_events("25152")) == before + 1


def test_event_text_does_not_depend_on_the_row_being_displayed(bot):
    """Une ligne repliee doit rendre le meme texte qu une ligne affichee.

    `innerText` ne le garantit pas : la spec HTML le fait retomber sur `textContent` pour un
    element non rendu, donc une ligne visible rend « Type<TAB>Date » et la meme ligne repliee
    « TypeDate ». Tout ce qui s appuie dessus — reference d offre, signature d evenement —
    deviendrait dependant du rendu.
    """
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    displayed = app_page.list_events("25152")
    assert displayed, "il faut au moins une ligne d evenement pour que le test prouve quelque chose"

    bot.page.evaluate(
        "() => Array.from(document.querySelectorAll('tr.trChildrenEvent'))"
        "        .forEach(r => { r.style.display = 'none'; })"
    )
    folded = app_page.list_events("25152")

    assert folded == displayed


def test_a_comment_wiped_by_a_postback_is_typed_again(bot):
    """Le choix du type recharge l'iframe : si le postback atterrit apres la saisie, le champ est
    vide et le commentaire partirait vide. Constate en recette — 0 caractere relu pour 49.

    Saisir un champ n'ecrit rien tant que rien n'est valide : on peut donc re-resoudre le champ
    et recommencer, sans risquer la moindre mutation.
    """
    bot._server.wipe_first_comment = True
    app_page, _ = bot.open_application("candidat@example.com", "25152")

    result = bot.add_event(app_page, "25152", "En attente", "Synthese Hippolyte.ai", "2026-09-25")

    assert result["ok"] is True, result
    assert result["verified"] is True


def test_a_truncated_comment_is_not_retried_and_says_so(bot):
    """Tronque n'est pas vide : le champ a bien recu la saisie, elle ne tient pas. Le code doit
    envoyer raccourcir, pas rejouer."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")

    result = bot.add_event(app_page, "25152", "En attente", "x" * 2500, "2026-09-25")

    assert result["ok"] is False
    assert result["error"] == "comment_too_long"
    assert result.get("mutation_started") is not True


def test_comment_longer_than_field_is_refused_without_mutating(bot):
    """Le champ tronquerait silencieusement : on refuse avant d'écrire."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = len(app_page.list_events("25152"))
    result = bot.add_event(app_page, "25152", "En attente", "x" * 2500, "2026-09-14")
    assert result["ok"] is False
    assert result["error"] == "comment_too_long"
    assert result.get("mutation_started") is not True
    assert len(app_page.list_events("25152")) == before


def test_event_types_referential_is_read_from_the_form(bot):
    types = bot.read_event_types("candidat@example.com", "25152")
    labels = [t["label"] for t in types]
    assert "Candidature à l'étude" in labels
    assert all(t["code"] for t in types)


# --- Pièces jointes -------------------------------------------------------------------------


def test_document_is_uploaded_in_a_free_category_and_verified(bot, tmp_path):
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    path = _pdf(tmp_path, "synthese.pdf")
    results = bot.add_documents(app_page, [path], "Autres documents")
    assert results[0]["ok"] is True, results
    assert results[0]["verified"] is True
    labels = [label.lower() for label in app_page.list_attachments()]
    assert any("synthese.pdf" in label and "autres documents" in label for label in labels)


def test_refuses_to_overwrite_an_occupied_category(bot, tmp_path):
    """LE garde-fou : déposer en catégorie occupée détruit le document du candidat."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = app_page.list_attachments()
    path = _pdf(tmp_path, "autre-cv.pdf")

    results = bot.add_documents(app_page, [path], "CV")

    assert results[0]["ok"] is False
    assert results[0]["error"] == "category_occupied"
    assert results[0].get("mutation_started") is not True
    # Le document d'origine est intact.
    assert app_page.list_attachments() == before
    assert any("CV CANDIDAT" in label for label in app_page.list_attachments())


def test_identical_document_is_skipped_not_redeposited(bot, tmp_path):
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    path = _pdf(tmp_path, "rapport.pdf")
    first = bot.add_documents(app_page, [path], "Compte rendu")
    assert first[0]["ok"] is True, first
    second = bot.add_documents(app_page, [path], "Compte rendu")
    assert second[0]["ok"] is True
    assert second[0]["skipped"] is True
    assert second[0]["reason"] == "already_present"


def test_several_documents_in_one_category_are_refused(bot, tmp_path):
    """Un seul champ par catégorie : le second fichier écraserait le premier."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    paths = [_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")]
    results = bot.add_documents(app_page, paths, "Autres documents")
    assert all(r["ok"] is False for r in results)
    assert all(r["error"] == "multiple_documents_same_category" for r in results)


def test_document_falls_back_to_the_first_free_category(bot, tmp_path):
    """Liste ordonnée : « Compte rendu » occupé ⇒ dépôt en « Compte rendu 2 », catégorie utilisée rendue."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    fallbacks = ["Compte rendu", "Compte rendu 2", "Compte rendu 3"]

    first = bot.add_documents(app_page, [_pdf(tmp_path, "synthese.pdf")], fallbacks)
    assert first[0]["ok"] is True, first
    assert first[0]["category"] == "Compte rendu"
    assert first[0]["categories_tried"] == ["Compte rendu"]

    second = bot.add_documents(app_page, [_pdf(tmp_path, "synthese-v2.pdf")], fallbacks)
    assert second[0]["ok"] is True, second
    assert second[0]["verified"] is True
    assert second[0]["category"] == "Compte rendu 2"
    assert second[0]["categories_tried"] == ["Compte rendu", "Compte rendu 2"]
    labels = [label.lower() for label in app_page.list_attachments()]
    assert any("synthese-v2.pdf" in label and "compte rendu 2" in label for label in labels)
    # Le document de « Compte rendu » est intact.
    assert any("synthese.pdf" in label and "(compte rendu)" in label for label in labels)


def test_document_already_present_in_a_fallback_is_skipped_not_redeposited(bot, tmp_path):
    """Passe 1 sur toute la liste : le fichier déjà en « Compte rendu 2 » ne repart pas en « Compte rendu »."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    path = _pdf(tmp_path, "rapport.pdf")
    placed = bot.add_documents(app_page, [path], ["Compte rendu 2"])
    assert placed[0]["ok"] is True

    again = bot.add_documents(app_page, [path], ["Compte rendu", "Compte rendu 2"])
    assert again[0]["ok"] is True
    assert again[0]["skipped"] is True
    assert again[0]["reason"] == "already_present"
    assert again[0]["category"] == "Compte rendu 2"
    assert again[0].get("mutation_started") is not True
    # « Compte rendu » reste libre : aucun doublon.
    assert not any("(compte rendu)" in label.lower() for label in app_page.list_attachments())


def test_all_fallback_categories_occupied_refuses_with_details(bot, tmp_path):
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    assert bot.add_documents(app_page, [_pdf(tmp_path, "a.pdf")], ["Compte rendu"])[0]["ok"] is True
    assert bot.add_documents(app_page, [_pdf(tmp_path, "b.pdf")], ["Compte rendu 2"])[0]["ok"] is True
    before = app_page.list_attachments()

    results = bot.add_documents(app_page, [_pdf(tmp_path, "c.pdf")], ["Compte rendu", "Compte rendu 2"])

    assert results[0]["ok"] is False
    assert results[0]["error"] == "category_occupied"
    assert results[0]["category"] == "Compte rendu"
    assert results[0]["categories_tried"] == ["Compte rendu", "Compte rendu 2"]
    assert results[0]["occupied_by"] == ["A.PDF"]
    assert results[0]["occupied_by_category"] == {"Compte rendu": ["A.PDF"], "Compte rendu 2": ["B.PDF"]}
    assert results[0].get("mutation_started") is not True
    assert app_page.list_attachments() == before


def test_category_absent_from_the_form_is_never_matched_by_substring(bot, tmp_path):
    """« Compte rend » n'existe pas : on n'écrit rien, au lieu de viser « Compte rendu » par sous-chaîne."""
    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = app_page.list_attachments()

    results = bot.add_documents(app_page, [_pdf(tmp_path, "x.pdf")], "Compte rend")

    assert results[0]["ok"] is False
    assert results[0]["error"] == "category_not_found"
    assert results[0].get("mutation_started") is not True
    assert app_page.list_attachments() == before


def test_update_application_forwards_the_ordered_category_list(bot, tmp_path):
    payload = bot.update_application(
        candidate_email="candidat@example.com",
        offer_id="25152",
        document_paths=[_pdf(tmp_path, "liste.pdf")],
        document_category="CV",
        document_categories=["CV", "Autres documents"],
    )
    documents = payload["actions"]["documents"]
    assert documents[0]["ok"] is True
    # « CV » est occupé par le CV du candidat : repli sur « Autres documents ».
    assert documents[0]["category"] == "Autres documents"
    assert documents[0]["categories_tried"] == ["CV", "Autres documents"]


def test_document_categories_referential_is_read_from_the_form(bot):
    categories = bot.read_document_categories("candidat@example.com", "25152")
    assert "CV" in categories
    assert "Autres documents" in categories


# --- Job complet ------------------------------------------------------------------------------


def test_update_application_combines_event_and_document(bot, tmp_path):
    path = _pdf(tmp_path, "bilan.pdf")
    payload = bot.update_application(
        candidate_email="candidat@example.com",
        offer_id="25152",
        event_type="En attente",
        comment="Synthèse complète",
        event_date="2026-09-14",
        document_paths=[path],
        document_category="Autres documents",
    )
    assert payload["mutation_started"] is True
    assert payload["actions"]["event"]["ok"] is True
    assert payload["actions"]["documents"][0]["ok"] is True
    # L'email ne doit jamais apparaître en clair dans la réponse.
    assert "candidat@example.com" not in str(payload)


def test_mutation_is_announced_once_before_the_first_write(bot, tmp_path):
    """Le drapeau anti-rejeu doit tomber à la première écriture, pas au lancement du job.

    Un événement puis un document, c'est deux soumissions : l'appelant ne doit être prévenu
    qu'une fois, sans quoi il croirait à deux mutations distinctes.
    """
    calls = []
    payload = bot.update_application(
        candidate_email="candidat@example.com",
        offer_id="25152",
        event_type="En attente",
        comment="Avec notification",
        event_date="2026-09-14",
        document_paths=[_pdf(tmp_path, "notifie.pdf")],
        document_category="Autres documents",
        on_mutation_started=lambda: calls.append("go"),
    )
    assert payload["mutation_started"] is True
    assert calls == ["go"]


def test_a_refused_comment_never_announces_a_mutation(bot):
    """Commentaire trop long : rien n'est écrit, donc le job doit rester rejouable."""
    calls = []
    payload = bot.update_application(
        candidate_email="candidat@example.com",
        offer_id="25152",
        event_type="En attente",
        comment="x" * 2500,
        event_date="2026-09-14",
        on_mutation_started=lambda: calls.append("go"),
    )
    assert calls == []
    assert payload["mutation_started"] is False
    assert payload["actions"]["event"]["error"] == "comment_too_long"


def test_account_can_be_chosen_by_identifier(bot_env, monkeypatch):
    """Le compte doit pouvoir être désigné par son identifiant ASCII.

    Les libellés du tenant sont accentués (« Accès @rtémis … ») : les transporter dans un
    .env est fragile. La `value` du radio (`airfrance.fr`) est un identifiant sûr.
    """
    from app.scraper import TalentsoftBot

    monkeypatch.setenv("TS_ACCOUNT_CHOICE", "airfrance.fr")
    instance = TalentsoftBot()
    server = FakeServer()
    server.install(instance.context)
    try:
        instance.ensure_logged_in()
        assert server.account_chosen is True
        assert instance.is_authenticated()
    finally:
        instance.close()


def test_unknown_account_choice_fails_with_a_usable_message(bot_env, monkeypatch):
    """Un échec de choix doit dire ce qui a été cherché : sans cela, il est indiagnosticable."""
    from app.scraper import TalentsoftBot
    from app.ts_pages import LoginError

    monkeypatch.setenv("TS_ACCOUNT_CHOICE", "compte-qui-n-existe-pas")
    instance = TalentsoftBot()
    server = FakeServer()
    server.install(instance.context)
    try:
        with pytest.raises(LoginError) as exc:
            instance.ensure_logged_in()
        assert "account_choice_not_found" in str(exc.value)
        assert "compte-qui-n-existe-pas" in str(exc.value)
    finally:
        instance.close()


def test_search_result_is_checked_against_the_requested_email(bot):
    """Une suggestion portant une autre adresse ne doit jamais etre ouverte.

    Ouvrir le dossier d un autre candidat serait une divulgation de donnees personnelles,
    pas une simple erreur de ciblage.
    """
    from app.ts_pages import AmbiguousCandidate, GlobalSearch

    bot.page.goto(BASE + "/", wait_until="domcontentloaded")
    search = GlobalSearch(bot.page, bot.deadline, 5000)
    search.search("quelquun.dautre@example.com")
    # La suggestion du faux Back Office reprend le terme cherche : on demande une AUTRE
    # adresse que celle affichee, le bot doit refuser plutot que cliquer.
    with pytest.raises(AmbiguousCandidate):
        search.open_single_result("candidat@example.com")


def test_search_result_matching_the_email_is_opened(bot):
    from app.ts_pages import GlobalSearch

    bot.page.goto(BASE + "/", wait_until="domcontentloaded")
    search = GlobalSearch(bot.page, bot.deadline, 5000)
    search.search("candidat@example.com")
    assert search.open_single_result("candidat@example.com") is True


def test_mail_dialog_is_refused_and_closed_if_it_ever_opens(bot):
    """Filet de securite : si un parcours d envoi de courrier s ouvre, ne jamais le valider.

    Le bot n emprunte plus les actions du panneau Outils, donc ce cas ne devrait plus se
    produire. Mais le bouton « Correspondre avec le candidat » est le voisin immediat de celui
    qu il clique, sur la meme ligne : la detection reste indispensable.
    """
    from app.ts_pages import EventDialog, MailDialogOpened

    app_page, _ = bot.open_application("candidat@example.com", "25152")
    before = len(app_page.list_events("25152"))

    # On ouvre deliberement le mauvais bouton, celui que le bot doit eviter.
    row = bot.page.locator("tr.selectedLine")
    row.locator("a[id$='btnSendMailNew']").click()

    dialog = EventDialog(bot.page, bot.deadline, 5000)
    with pytest.raises(MailDialogOpened):
        dialog.wait_open()

    # Aucun courrier envoye, aucun evenement cree, et la modale a ete refermee.
    assert bot.page.evaluate("() => !!window.__mailWasSent") is False
    assert len(app_page.list_events("25152")) == before
    assert bot.page.locator("iframe[src*='ActionMailLanguageChoicePage']").count() == 0


def test_bot_never_clicks_the_correspondence_button(bot):
    """Le bouton de courrier est le voisin de celui du bot : verifier qu ils sont distincts."""
    from app import ts_selectors

    assert ts_selectors.EVENT_ACTION_BUTTON != ts_selectors.ROW_SEND_MAIL_BUTTON
    assert all("btnEventActionNew" in c for c in ts_selectors.EVENT_ACTION_BUTTON)
    assert all("btnSendMailNew" in c for c in ts_selectors.ROW_SEND_MAIL_BUTTON)


def test_event_submit_selector_never_matches_a_mail_send_button(bot):
    """Garde-fou de selecteur : valider ne doit jamais se faire sur une classe generique.

    `input.valid-button` designe aussi le bouton d envoi de courrier : il ne doit plus
    figurer parmi les candidats de validation.
    """
    from app import ts_selectors

    assert "input.valid-button" not in ts_selectors.EVENT_SUBMIT
    assert "input.valid-button" not in ts_selectors.ATTACHMENT_SUBMIT
    assert all("btValidate" in candidate for candidate in ts_selectors.EVENT_SUBMIT)
    assert all("btValidate" in candidate for candidate in ts_selectors.ATTACHMENT_SUBMIT)


def test_session_expiring_to_the_collaborator_space_is_detected(bot):
    """Une session Back Office expiree renvoie vers l espace collaborateur, pas vers un login.

    Sans detection, le bot se croirait connecte puis echouerait sur une barre de recherche
    introuvable — un symptome qui ne designe pas sa cause.
    """
    from app.scraper import SessionExpired

    bot._server.expire_to_landing = True
    with pytest.raises(SessionExpired):
        bot.open_application("candidat@example.com", "25152")


def test_restored_session_landing_outside_the_back_office_is_resumed(bot_env):
    """Demarrage a froid avec des cookies encore valides.

    Le tenant renvoie la racine du Back Office vers l espace collaborateur tant que la session
    applicative du BO n est pas ouverte. Il faut donc y ENTRER, pas se reauthentifier : l IdP
    nous tient pour connecte et ne presente aucun formulaire. Sans cela le bot echouait sur
    `login_form_not_found` en quelques secondes, sans trace exploitable — constate en recette.
    """
    from app.scraper import TalentsoftBot

    instance = TalentsoftBot()
    server = FakeServer()
    # Session deja ouverte cote fournisseur d identite, session du Back Office fermee.
    server.credentials_posted = True
    server.install(instance.context)
    try:
        instance.ensure_logged_in()
        assert instance.is_authenticated()
        assert server.back_office_opened is True, "le point d entree du Back Office n a pas ete emprunte"
        # Et surtout : aucune reauthentification. Reposter les identifiants ici serait inutile
        # et ferait grimper le compteur d echecs de login pour rien.
        assert not any(url.startswith(IDP) for url in server.requests)
    finally:
        instance.close()


def test_unusable_restored_session_is_dropped_before_logging_in(bot_env):
    """Cookies perimes : il faut vider le contexte, pas seulement le fichier.

    Les cookies du storage_state sont deja charges dans le contexte courant. Les laisser en
    place ferait echouer le login complet exactement de la meme facon.
    """
    from app import config
    from app.scraper import TalentsoftBot

    state = Path(config.storage_state_path())
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    instance = TalentsoftBot()
    server = FakeServer()
    server.stale_restored_session = True
    server.install(instance.context)
    try:
        instance.ensure_logged_in()
        assert instance.is_authenticated()
        # Le parcours de connexion complet a bien eu lieu apres l abandon.
        assert server.account_chosen is True
        assert server.credentials_posted is True
    finally:
        instance.close()


def test_back_office_presence_is_checked_by_origin(bot):
    """Le controle de presence sur le Back Office repose sur l origine, pas sur un marqueur."""
    assert bot._on_back_office() is True
    bot.page.goto(LANDING + "/MyTalentsoft", wait_until="domcontentloaded")
    assert bot._on_back_office() is False


def test_headless_never_announces_itself_as_headless(bot):
    """Des fournisseurs d identite refusent « HeadlessChrome », sans message d erreur.

    Le formulaire est alors accepte mais l authentification n aboutit pas : un echec muet,
    tres couteux a diagnostiquer. Le contexte doit donc presenter un user-agent de bureau.
    """
    user_agent = bot.page.evaluate("() => navigator.userAgent")
    assert "Headless" not in user_agent, user_agent
    assert "Chrome" in user_agent


def test_user_agent_can_be_overridden(bot_env, monkeypatch):
    """Un tenant peut exiger un user-agent particulier : il reste configurable."""
    from app.scraper import TalentsoftBot

    monkeypatch.setenv("BROWSER_USER_AGENT", "Mozilla/5.0 (Test) AgentPersonnalise/1.0")
    instance = TalentsoftBot()
    try:
        assert instance.page.evaluate("() => navigator.userAgent") == "Mozilla/5.0 (Test) AgentPersonnalise/1.0"
    finally:
        instance.close()


def test_discarding_the_persisted_session_removes_the_file(bot_env):
    """Un `storage_state` perime porte le cookie qui accompagne le jeton anti-CSRF.

    S il ne correspond plus, le serveur reaffiche le formulaire SANS message — indiscernable
    d un mot de passe refuse. Le jeter apres un echec evite de propager la panne.
    """
    import os

    from app import config
    from app.scraper import TalentsoftBot

    instance = TalentsoftBot()
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(config.storage_state_path(), "w", encoding="utf-8") as handle:
            handle.write('{"cookies": [], "origins": []}')
        assert os.path.exists(config.storage_state_path())

        instance.discard_storage_state()
        assert not os.path.exists(config.storage_state_path())
        # Idempotent : un second appel ne doit pas lever.
        instance.discard_storage_state()
    finally:
        instance.close()


def test_login_submits_even_when_the_button_does_nothing(bot_env):
    """Le bouton « Connexion » de l IdP ne soumet pas : un autre geste doit prendre le relais.

    Constate en production — la capture d ecran de l echec montrait les DEUX champs encore
    renseignes, alors qu un POST rejete reaffiche la page avec le mot de passe vide.
    """
    from app.scraper import TalentsoftBot

    instance = TalentsoftBot()
    server = FakeServer()
    server.install(instance.context)
    try:
        instance.ensure_logged_in()
        # Le formulaire a bien ete soumis malgre le bouton neutralise.
        assert server.credentials_posted is True
        assert instance.is_authenticated()
    finally:
        instance.close()


def test_back_office_is_reached_through_its_entry_point(bot):
    """Viser le Back Office par son URL ne suffit pas apres le SSO.

    Tant que sa session applicative n est pas ouverte, sa racine renvoie vers l espace
    collaborateur. C est le lien « Recrutement » (RedirectBackOffice.ashx), servi par l hote
    d atterrissage, qui l ouvre.
    """
    assert bot._server.back_office_opened is True, "le point d entree n a pas ete emprunte"
    assert bot._on_back_office() is True
    assert bot.is_authenticated()
