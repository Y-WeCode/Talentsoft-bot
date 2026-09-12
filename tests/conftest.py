import importlib
import sys

import pytest


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    """Charge app.main dans un environnement de test isolé (config lue à l'import)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setenv("API_TOKEN_PREVIOUS", "old-token")
    monkeypatch.setenv("TS_BASE_URL", "https://tenant.talent-soft.com")
    monkeypatch.setenv("TS_USERNAME", "bot@example.com")
    monkeypatch.setenv("TS_PASSWORD", "secret")
    monkeypatch.setenv("TS_AUTH_HOSTS", "fedauth.talent-soft.com,idp.talent-soft.com")
    monkeypatch.setenv("TS_SELFTEST_CANDIDATE_EMAIL", "temoin@example.com")
    monkeypatch.setenv("TS_SELFTEST_OFFER_ID", "25152")
    monkeypatch.setenv("ENABLE_API_DOCS", "false")
    monkeypatch.setenv("BROWSER_RETRY_AFTER_SECONDS", "60")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TS_ASYNC_JOBS_ENABLED", raising=False)

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)

    module = importlib.import_module("app.main")
    from app import idempotency

    idempotency.reset_memory_store()
    return module


class FakeBot:
    """Double du TalentsoftBot : aucun navigateur."""

    def __init__(self, actions=None):
        self.closed = False
        self.update_calls = 0
        self.last_kwargs = None
        self._authenticated = True
        self._actions = actions if actions is not None else {"event": {"ok": True, "verified": True}}

    def is_alive(self):
        return not self.closed

    def is_authenticated(self):
        return True

    def ensure_logged_in(self):
        return None

    def update_application(self, **kwargs):
        self.update_calls += 1
        self.last_kwargs = kwargs
        return {
            "candidate_email_hash": "hash",
            "offer_id": kwargs["offer_id"],
            "application_label": "reponse a offre ( ref. 2026-25152)",
            "updated_at": "2026-01-01T00:00:00",
            "mutation_started": True,
            "actions": self._actions,
        }

    def list_events(self, candidate_email, offer_id):
        return ["evenement 1"]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_bot(app_module, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    monkeypatch.setattr(app_module.session_manager, "invalidate", lambda *a, **k: None)
    return bot


AUTH = {"Authorization": "Bearer test-token"}
