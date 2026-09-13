"""Contrat HTTP quand le worker possède le navigateur.

L'invariant que ces tests protègent : **l'API n'ouvre jamais de navigateur dans ce mode**. La
fixture `worker_mode` remplace `TalentsoftBot` par un constructeur qui échoue, si bien qu'une
régression se manifeste par un test rouge plutôt que par des déconnexions en recette.
"""

from __future__ import annotations

import threading

import pytest
from conftest import AUTH, FakeBot, pump_worker
from fastapi.testclient import TestClient

PUSH = {"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "Résultat"}


@pytest.fixture
def client(worker_mode, app_module):
    return TestClient(app_module.app)


def _install_bot(monkeypatch, bot):
    from app import session_manager as sm_module

    monkeypatch.setattr(sm_module.session_manager, "get_bot", lambda: bot)
    monkeypatch.setattr(sm_module.session_manager, "invalidate", lambda *a, **k: None)
    return bot


def _pump_when_queued(stop: threading.Event) -> threading.Thread:
    """Fait tourner le worker en arrière-plan le temps d'un appel synchrone."""

    def loop():
        from app import jobs

        while not stop.is_set():
            item = jobs.brpop_next_job(timeout_seconds=0)
            if item:
                pump_worker(item[1])
                return
            stop.wait(0.01)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


# --- Invariant ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/update-application", {"data": PUSH}),
        ("post", "/applications/events", {"json": PUSH}),
        ("get", "/applications/events?candidate_email=c@example.com&offer_id=25152", {}),
        ("get", "/referentials/event-types", {}),
        ("get", "/referentials/document-categories", {}),
        ("post", "/selftest", {}),
    ],
)
def test_api_never_opens_a_browser_in_worker_mode(client, method, path, kwargs):
    """Sans personne pour dépiler, toutes les routes doivent patienter puis rendre la main —
    jamais ouvrir un Chromium dans le processus API."""
    response = getattr(client, method)(path, headers=AUTH, **kwargs)
    assert response.status_code in (202, 503), response.text


# --- Disponibilité du worker ----------------------------------------------------------------


def test_missing_heartbeat_returns_503_and_enqueues_nothing(client, worker_mode):
    from app import jobs

    worker_mode.kv.pop(jobs.WORKER_HEARTBEAT_KEY, None)
    response = client.post("/update-application", data=PUSH, headers=AUTH)

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert worker_mode.llen(jobs.QUEUE_PUSH) == 0


def test_degraded_worker_returns_503_and_enqueues_nothing(client, worker_mode):
    from app import jobs

    jobs.worker_heartbeat({"alive": True, "degraded": True, "degraded_reason": "login_failures:LoginError"})
    response = client.post("/update-application", data=PUSH, headers=AUTH)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "600"
    assert worker_mode.llen(jobs.QUEUE_PUSH) == 0


# --- Attente synchrone ----------------------------------------------------------------------


def test_sync_push_returns_200_when_the_worker_answers_in_time(client, monkeypatch):
    _install_bot(monkeypatch, FakeBot())
    stop = threading.Event()
    thread = _pump_when_queued(stop)
    try:
        response = client.post("/update-application", data=PUSH, headers=AUTH)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_wait_timeout_degrades_to_202_with_a_job_to_follow(client, monkeypatch):
    """Budget d'attente dépassé : 202, jamais un 5xx qui inviterait à rejouer une écriture."""
    from app import config

    monkeypatch.setattr(config, "sync_wait_timeout_seconds", lambda: 1)
    response = client.post("/update-application", data=PUSH, headers=AUTH)

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert response.headers["Location"] == f"/jobs/{body['job_id']}"


def test_replaying_after_a_202_returns_the_same_job(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "sync_wait_timeout_seconds", lambda: 1)
    payload = {**PUSH, "idempotency_key": "kk"}
    first = client.post("/update-application", data=payload, headers=AUTH)
    second = client.post("/update-application", data=payload, headers=AUTH)

    assert first.status_code == 202
    # Un 409 dirait « une requete identique tourne » sans dire laquelle : inexploitable.
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]


def test_explicit_async_never_creates_two_jobs_for_one_key(client, worker_mode):
    """Sans réservation de la clé, un appel sync et un job async pouvaient muter en parallèle."""
    from app import jobs

    payload = {**PUSH, "idempotency_key": "async-k"}
    first = client.post("/update-application?async=1", data=payload, headers=AUTH)
    second = client.post("/update-application?async=1", data=payload, headers=AUTH)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert worker_mode.llen(jobs.QUEUE_PUSH) == 1


def test_failed_job_maps_its_code_to_a_status(client, monkeypatch):
    from app.ts_pages import CandidateNotFound

    class EmptyBot(FakeBot):
        def update_application(self, **kwargs):
            raise CandidateNotFound("introuvable")

    _install_bot(monkeypatch, EmptyBot())
    stop = threading.Event()
    thread = _pump_when_queued(stop)
    try:
        response = client.post("/update-application", data=PUSH, headers=AUTH)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert response.status_code == 404
    assert response.json() == {"detail": "Candidat introuvable"}


# --- Healthcheck ----------------------------------------------------------------------------


def test_health_reports_the_worker_state(client):
    body = client.get("/").json()

    assert body["browser_owner"] == "worker"
    assert body["worker"]["alive"] is True
    assert body["degraded"] is False
    assert body["queues"] == {"read": 0, "push": 0}


def test_health_stays_green_when_redis_is_unreachable(client, monkeypatch):
    """Un healthcheck qui tombe avec Redis ne sert à rien : il doit dire que Redis est absent."""
    from app import jobs

    def unreachable():
        raise ConnectionError("redis down")

    monkeypatch.setattr(jobs, "_redis", unreachable)
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["worker"] == {"alive": False, "reason": "redis_unreachable"}


def test_health_reports_an_expired_heartbeat(client, worker_mode):
    from app import jobs

    worker_mode.kv.pop(jobs.WORKER_HEARTBEAT_KEY, None)
    assert client.get("/").json()["worker"]["reason"] == "heartbeat_expired"


# --- Administration -------------------------------------------------------------------------


def test_reset_session_is_delegated_to_the_worker(client, worker_mode):
    from app import jobs

    response = client.post("/admin/reset-session", headers=AUTH)

    assert response.status_code == 202
    assert response.json()["job_id"]
    # Enfile en LECTURE : sortir de l'etat degrade doit marcher meme file de pushs pleine.
    assert worker_mode.llen(jobs.QUEUE_READ) == 1
