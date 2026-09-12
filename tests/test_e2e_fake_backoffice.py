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
        self.blocked: list[str] = []

    def install(self, context):
        context.route("**/*", self.handle)

    def handle(self, route, request):
        url = request.url
        self.requests.append(url)
        cookies = request.headers.get("cookie", "")
        logged_in = "ts_session=1" in cookies and not self.reset_session

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

        if not logged_in:
            return route.fulfill(status=200, content_type="text/html", body=_page("account-choice.html"))

        if path == "/Pages/Applicants/MainPage.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("applicant.html"))
        if path == "/Pages/Applicants.Events/JobApplicationChildEventEdit.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("event-dialog.html"))
        if path == "/Pages/Utils/AttachedFileEdit.aspx":
            return route.fulfill(status=200, content_type="text/html", body=_page("attachment-dialog.html"))
        return route.fulfill(status=200, content_type="text/html", body=_page("home.html"))


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_BASE_URL", BASE)
    monkeypatch.setenv("TS_AUTH_HOSTS", "idp-fake.talent-soft.com")
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
    """Le parcours à trois hôtes doit aboutir, sans quoi le bot ne peut rien faire."""
    assert bot._server.account_chosen is True
    assert any(url.startswith(IDP) for url in bot._server.requests)
    assert bot.is_authenticated()


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
