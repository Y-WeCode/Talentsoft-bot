import pytest

from app import session_manager as sm_module
from app.session_manager import SessionBootstrapError, SessionDegradedError, SessionManager


class FakeBot:
    def __init__(self, fail_login=False):
        self.closed = False
        self.login_calls = 0
        self._authenticated = False
        self.fail_login = fail_login

    def is_alive(self):
        return not self.closed

    def is_authenticated(self):
        return self._authenticated

    def ensure_logged_in(self):
        if isinstance(self.fail_login, Exception):
            raise self.fail_login
        if self.fail_login:
            raise RuntimeError("login_failed")
        if not self._authenticated:
            self.login_calls += 1
            self._authenticated = True

    def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_BASE_URL", "https://tenant.talent-soft.com")


@pytest.fixture
def manager(monkeypatch, env):
    mgr = SessionManager()
    created = []

    def fake_ctor():
        bot = FakeBot(fail_login=getattr(mgr, "_fail_login", False))
        created.append(bot)
        return bot

    monkeypatch.setattr(sm_module.scraper_module, "TalentsoftBot", fake_ctor)
    mgr._created = created
    return mgr


def test_reuses_session_without_second_login(manager):
    first = manager.get_bot()
    second = manager.get_bot()
    assert first is second
    assert manager.login_count == 1
    assert len(manager._created) == 1


def test_recycles_when_idle_ttl_exceeded(manager, monkeypatch):
    first = manager.get_bot()
    manager._state.last_used_at -= 10_000
    second = manager.get_bot()
    assert first is not second
    assert first.closed is True
    assert manager.login_count == 2


def test_recycles_when_browser_dead(manager):
    first = manager.get_bot()
    first.closed = True
    second = manager.get_bot()
    assert second is not first


def test_request_invalidate_is_applied_on_next_get(manager):
    first = manager.get_bot()
    manager.request_invalidate("test")
    second = manager.get_bot()
    assert first.closed is True and second is not first


def test_degraded_after_repeated_login_failures(manager, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "2")
    manager._fail_login = True
    for _ in range(2):
        with pytest.raises(SessionBootstrapError):
            manager.get_bot()
    assert manager.is_degraded() is True
    with pytest.raises(SessionDegradedError):
        manager.get_bot()
    status = manager.status()
    assert status["degraded"] is True and status["session_open"] is False

    manager.reset_degraded()
    manager._fail_login = False
    bot = manager.get_bot()
    assert bot._authenticated is True
    assert manager.is_degraded() is False


def test_bootstrap_error_never_leaks_message(manager):
    """Une exception tierce ne doit jamais propager son message : il peut porter une URL,
    du HTML ou une valeur saisie. Seul le type remonte."""
    manager._fail_login = True
    with pytest.raises(SessionBootstrapError) as exc:
        manager.get_bot()
    assert "login_failed" not in str(exc.value)
    assert str(exc.value) == "RuntimeError"


def test_bootstrap_error_keeps_message_of_our_own_login_errors(manager):
    """Nos LoginError sont ecrites sans secret par contrat : leur message est conserve,
    sans quoi un echec de login est indiagnosticable en production."""
    from app.ts_pages import LoginError

    manager._fail_login = LoginError("account_choice_not_found: TS_ACCOUNT_CHOICE absent")
    with pytest.raises(SessionBootstrapError) as exc:
        manager.get_bot()
    assert "account_choice_not_found" in str(exc.value)
    assert str(exc.value).startswith("LoginError:")
