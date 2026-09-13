import importlib
import sys
import threading
import time

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


class FakeRedis:
    """Redis en mémoire, réduit à ce que `app.jobs` utilise.

    Une seule différence assumée avec le vrai Redis : `timeout=0` ne veut pas dire « bloquer
    indéfiniment » mais « ne pas attendre ». Un test qui se trompe rend donc la main au lieu de
    figer la suite.
    """

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    # --- clés ---------------------------------------------------------------------------
    def get(self, key):
        with self._lock:
            return self.kv.get(key)

    def set(self, key, value, ex=None, nx=False):
        with self._lock:
            if nx and key in self.kv:
                return None
            self.kv[key] = value
            return True

    def delete(self, key):
        with self._lock:
            self.kv.pop(key, None)

    def expire(self, key, seconds):
        return True

    # --- listes -------------------------------------------------------------------------
    def lpush(self, key, value):
        with self._lock:
            self.lists.setdefault(key, []).insert(0, value)
            return len(self.lists[key])

    def llen(self, key):
        with self._lock:
            return len(self.lists.get(key, []))

    def _pop(self, keys, timeout, index):
        """Attend qu'une des listes se remplisse. L'ordre des clés fait la priorité."""
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        while True:
            with self._lock:
                for key in keys:
                    items = self.lists.get(key)
                    if items:
                        return key, items.pop(index)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def brpop(self, keys, timeout=0):
        return self._pop(keys if isinstance(keys, list) else [keys], timeout, -1)

    def blpop(self, key, timeout=0):
        return self._pop(key if isinstance(key, list) else [key], timeout, 0)

    # --- pipeline -----------------------------------------------------------------------
    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self

        return record

    def execute(self):
        results = []
        for name, args, kwargs in self._ops:
            results.append(getattr(self._redis, name)(*args, **kwargs))
        self._ops.clear()
        return results


@pytest.fixture
def fake_redis(monkeypatch):
    """Branche `app.jobs` et l'idempotence sur une mémoire locale, sans serveur Redis."""
    from app import config, idempotency, jobs

    redis = FakeRedis()
    monkeypatch.setattr(jobs, "_redis", lambda: redis)
    monkeypatch.setattr(config, "async_jobs_enabled", lambda: True)
    # L'idempotence choisirait sinon un vrai client Redis dès que REDIS_URL est défini.
    monkeypatch.setattr(idempotency, "_store", lambda: idempotency._memory)
    idempotency.reset_memory_store()
    return redis


@pytest.fixture
def worker_mode(app_module, fake_redis, monkeypatch):
    """Place l'API en mode worker : elle n'ouvre plus de navigateur, elle empile.

    Le battement de cœur est publié d'emblée, sinon toutes les routes répondraient 503.
    """
    from app import config, jobs, scraper

    monkeypatch.setattr(config, "browser_owner", lambda: "worker")

    def no_browser_in_api(*args, **kwargs):
        raise AssertionError("l'API a ouvert un navigateur alors que le worker en est propriétaire")

    monkeypatch.setattr(scraper, "TalentsoftBot", no_browser_in_api)
    # Budgets d attente courts : un test sans worker ne doit pas immobiliser la suite.
    monkeypatch.setenv("SYNC_WAIT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("SYNC_READ_WAIT_TIMEOUT_SECONDS", "1")
    jobs.worker_heartbeat({"alive": True, "session_open": True, "degraded": False})
    return fake_redis


def pump_worker(job_id=None):
    """Dépile et exécute un job comme le ferait le worker, sans thread ni processus."""
    from app import jobs
    from app import worker as worker_module

    if job_id is None:
        item = jobs.brpop_next_job(timeout_seconds=0)
        assert item, "aucun job dans la file"
        job_id = item[1]
    job = jobs.get_job(job_id)
    assert job, f"job {job_id} introuvable"
    worker_module._process_job(job)
    return jobs.get_job(job_id)
