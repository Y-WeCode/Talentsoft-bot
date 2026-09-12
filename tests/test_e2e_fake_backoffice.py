"""Bout en bout contre un faux Back Office servi par interception réseau Playwright.

Valide le code navigateur réel (launch, storage_state, route guard, fill, select, set_input_files)
sans dépendre du tenant Talentsoft. Sauté si aucun Chromium n'est disponible.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "fake_backoffice"
BASE = "https://fake.talent-soft.com"


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


class FakeServer:
    """Sert les pages du faux Back Office et simule la session par cookie."""

    def __init__(self):
        self.requests: list[str] = []
        self.reset_session = False

    def install(self, page):
        page.route(f"{BASE}/**", self.handle)

    def handle(self, route, request):
        url = request.url
        self.requests.append(url)
        path = url[len(BASE) :].split("?")[0]
        cookies = request.headers.get("cookie", "")
        logged_in = "ts_session=1" in cookies and not self.reset_session
        if path in ("/", "/Account/Logon", "/Account/LogOff"):
            if path == "/Account/LogOff":
                return route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=(FIXTURES / "login.html").read_text(),
                    headers={"Set-Cookie": "ts_session=; Max-Age=0; path=/"},
                )
            if logged_in and path == "/":
                return route.fulfill(status=200, content_type="text/html", body=(FIXTURES / "home.html").read_text())
            return route.fulfill(status=200, content_type="text/html", body=(FIXTURES / "login.html").read_text())
        if not logged_in:
            return route.fulfill(status=200, content_type="text/html", body=(FIXTURES / "login.html").read_text())
        if path == "/Recruiting/BackOffice/Home":
            return route.fulfill(status=200, content_type="text/html", body=(FIXTURES / "home.html").read_text())
        if path.startswith("/Recruiting/BackOffice/Applications/"):
            app_id = path.rsplit("/", 1)[-1]
            if app_id == "404":
                return route.fulfill(
                    status=404, content_type="text/html", body=(FIXTURES / "notfound.html").read_text()
                )
            return route.fulfill(status=200, content_type="text/html", body=(FIXTURES / "application.html").read_text())
        return route.fulfill(status=404, content_type="text/html", body="<h1>404</h1>")


@pytest.fixture
def bot_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_BASE_URL", BASE)
    monkeypatch.setenv("TS_USERNAME", "bot@example.com")
    monkeypatch.setenv("TS_PASSWORD", "secret")
    monkeypatch.setenv("HEADLESS_MODE", "true")
    monkeypatch.setenv("ACTION_TIMEOUT_MS", "5000")
    monkeypatch.setenv("NAVIGATION_TIMEOUT_MS", "8000")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("TRACES_ENABLED", "true")
    monkeypatch.setenv("SCREENSHOTS_ENABLED", "true")
    monkeypatch.delenv("TS_APPLICATION_URL_TEMPLATE", raising=False)
    return tmp_path


@pytest.fixture
def bot(bot_env):
    from app.scraper import TalentsoftBot

    instance = TalentsoftBot()
    server = FakeServer()
    server.install(instance.page)
    instance._server = server
    yield instance
    instance.close()


def test_login_then_event_and_document_are_verified(bot, bot_env):
    from app import safety

    bot.ensure_logged_in()
    assert bot.is_authenticated() is True
    state = Path(bot_env) / "state" / "storage_state.json"
    assert state.exists()
    assert oct(state.stat().st_mode & 0o777) == "0o600"

    pdf = bot_env / "synthese_hippolyte.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = bot.update_application(
        application_id="12345",
        application_url=safety.build_application_url("12345"),
        event_type="Commentaire",
        comment="Synthèse Hippolyte.ai : profil retenu",
        event_date="2026-09-12",
        document_paths=[str(pdf)],
        document_category="Autre",
    )
    assert result["actions"]["event"] == {
        "ok": True,
        "event_type": "Commentaire",
        "mutation_started": True,
        "verified": True,
    }
    doc = result["actions"]["documents"][0]
    assert doc["ok"] is True and doc["verified"] is True and doc["category"] == "Autre"
    assert doc["filename"] == "synthese_hippolyte.pdf"
    assert result["mutation_started"] is True
    assert safety.actions_succeeded(result["actions"]) is True
    # Job réussi : aucune trace conservée.
    assert not list((bot_env / "traces").glob("*.zip")) if (bot_env / "traces").exists() else True


def test_second_identical_event_is_skipped(bot):
    from app import safety

    bot.ensure_logged_in()
    url = safety.build_application_url("777")
    first = bot.update_application(
        application_id="777", application_url=url, event_type="Commentaire", comment="Doublon test"
    )
    assert first["actions"]["event"]["ok"] is True
    # La fausse page repart de zéro à chaque navigation : on simule la persistance en réinjectant la ligne.
    bot.page.evaluate(
        "() => { const li = document.createElement('li'); li.textContent = 'x Commentaire : Doublon test'; document.getElementById('historyList').prepend(li); }"
    )
    from app import config
    from app.ts_pages import ApplicationPage

    page_obj = ApplicationPage(bot.page, bot.deadline, config.action_timeout_ms())
    second = bot.add_event(page_obj, "Commentaire", "Doublon test", None)
    assert second == {"ok": True, "skipped": True, "reason": "already_present", "event_type": "Commentaire"}


def test_application_not_found(bot):
    from app import safety
    from app.scraper import ApplicationNotFound

    bot.ensure_logged_in()
    with pytest.raises(ApplicationNotFound):
        bot.open_application(safety.build_application_url("404"))


def test_expired_session_relogins_before_mutation(bot):
    from app import safety

    bot.ensure_logged_in()
    server = bot._server
    # Expiration côté serveur : la prochaine navigation renvoie la page de login.
    server.reset_session = True
    bot.context.clear_cookies()
    server.reset_session = False
    app_page = bot.open_application(safety.build_application_url("1"))
    assert app_page.is_ready()
    assert any(u.startswith(f"{BASE}/Recruiting/BackOffice/Applications/1") for u in server.requests)


def test_navigation_outside_tenant_is_blocked(bot):
    from playwright.sync_api import Error

    bot.ensure_logged_in()
    with pytest.raises(Error):
        bot.page.goto("https://evil.example.com/steal", timeout=5000)


def test_wrong_password_raises_login_error_without_leaking(bot, monkeypatch):
    from app.ts_pages import LoginError

    monkeypatch.setenv("TS_PASSWORD", "wrong-password")
    with pytest.raises(LoginError) as exc:
        bot.ensure_logged_in()
    assert "wrong-password" not in str(exc.value)


def test_referentials_are_read_from_dialogs(bot):
    from app import safety

    bot.ensure_logged_in()
    url = safety.build_application_url("5")
    assert bot.read_event_types(url) == ["--", "Commentaire", "Entretien téléphonique"]
    assert bot.read_document_categories(url) == ["CV", "Lettre de motivation", "Autre"]


def test_selftest_reports_selectors(bot, monkeypatch):
    from app import safety

    bot.ensure_logged_in()
    report = bot.selftest(safety.build_application_url("9"))
    assert report["authenticated"] is True
    assert report["application_opened"] is True
    assert all(v["ok"] for v in report["selectors"].values()), report
    assert report["ok"] is True
