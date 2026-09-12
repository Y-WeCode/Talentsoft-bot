import io
import threading
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeBot

PDF = b"%PDF-1.4\n%fake\n"


def test_requires_token(app_module):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application", data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"}
    )
    assert response.status_code in (401, 403)


def test_previous_token_accepted_for_rotation(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers={"Authorization": "Bearer old-token"},
    )
    assert response.status_code == 200


def test_health_does_not_touch_browser(app_module):
    client = TestClient(app_module.app)
    body = client.get("/").json()
    assert body["status"] == "ok"
    assert body["browser_busy"] is False
    assert body["degraded"] is False


def test_update_application_success_contract(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={
            "candidate_email": "candidat@example.com",
            "offer_id": "25152",
            "comment": "Résultat Hippolyte.ai",
            "event_type": "Commentaire",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["update_details"]["offer_id"] == "25152"
    assert fake_bot.last_kwargs["comment"] == "Résultat Hippolyte.ai"
    assert fake_bot.closed is False


def test_update_application_success_false_when_action_fails(app_module, monkeypatch):
    bot = FakeBot(actions={"event": {"ok": False, "error": "event_failed"}})
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["success"] is False


def test_skipped_counts_as_success_including_document_lists(app_module, monkeypatch):
    bot = FakeBot(
        actions={
            "event": {"ok": True, "skipped": True, "reason": "already_present"},
            "documents": [{"ok": True, "skipped": True, "reason": "already_present"}, {"ok": True}],
        }
    )
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x", "idempotency_key": "abc"},
        files=[("documents", ("synthese.pdf", io.BytesIO(PDF), "application/pdf"))],
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert bot.last_kwargs["document_paths"][0].endswith("synthese.pdf")


def test_unverified_document_fails_success(app_module, monkeypatch):
    bot = FakeBot(actions={"documents": [{"ok": False, "error": "unverified", "mutation_may_have_happened": True}]})
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152"},
        files=[("documents", ("cv.pdf", io.BytesIO(PDF), "application/pdf"))],
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["update_details"]["actions"]["documents"][0]["mutation_may_have_happened"] is True


def test_rejects_when_nothing_to_do(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application", data={"candidate_email": "candidat@example.com", "offer_id": "25152"}, headers=AUTH
    )
    assert response.status_code == 400
    assert fake_bot.update_calls == 0


def test_rejects_invalid_candidate_email(app_module, fake_bot):
    """Un email malforme ne doit jamais partir dans la recherche du Back Office."""
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "pas-un-email", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert fake_bot.update_calls == 0


def test_rejects_invalid_offer_id(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "../../etc", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert fake_bot.update_calls == 0


def test_passes_email_and_offer_to_bot(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert fake_bot.last_kwargs["candidate_email"] == "candidat@example.com"
    assert fake_bot.last_kwargs["offer_id"] == "25152"


def test_rejects_file_with_wrong_magic_bytes(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152"},
        files=[("documents", ("malware.pdf", io.BytesIO(b"MZ\x90\x00 not a pdf"), "application/pdf"))],
        headers=AUTH,
    )
    assert response.status_code == 400
    assert fake_bot.update_calls == 0


def test_rejects_disallowed_extension(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152"},
        files=[("documents", ("script.exe", io.BytesIO(b"MZ"), "application/octet-stream"))],
        headers=AUTH,
    )
    assert response.status_code == 400


def test_comment_too_long_rejected(app_module, fake_bot, monkeypatch):
    monkeypatch.setenv("COMMENT_MAX_CHARS", "10")
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x" * 11},
        headers=AUTH,
    )
    assert response.status_code == 400


def test_invalid_event_date_rejected(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={
            "candidate_email": "candidat@example.com",
            "offer_id": "25152",
            "comment": "x",
            "event_date": "12/09/2026",
        },
        headers=AUTH,
    )
    assert response.status_code == 400


def test_sync_idempotency_replays_without_second_mutation(app_module, fake_bot):
    client = TestClient(app_module.app)
    data = {
        "candidate_email": "candidat@example.com",
        "offer_id": "25152",
        "comment": "same",
        "idempotency_key": "push-42",
    }
    first = client.post("/update-application", data=data, headers=AUTH)
    second = client.post("/update-application", data=data, headers=AUTH)
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.json() == first.json()
    assert fake_bot.update_calls == 1


def test_sync_idempotency_derived_from_content(app_module, fake_bot):
    client = TestClient(app_module.app)
    data = {
        "candidate_email": "candidat@example.com",
        "offer_id": "25152",
        "comment": "same content",
        "event_type": "Commentaire",
    }
    client.post("/update-application", data=data, headers=AUTH)
    client.post("/update-application", data=data, headers=AUTH)
    client.post("/update-application", data={**data, "comment": "other"}, headers=AUTH)
    assert fake_bot.update_calls == 2


def test_idempotency_key_released_when_mutation_did_not_start(app_module, monkeypatch):
    from app.scraper import ApplicationNotFound

    class NotFoundBot(FakeBot):
        def update_application(self, **kwargs):
            self.update_calls += 1
            raise ApplicationNotFound(kwargs["offer_id"])

    bot = NotFoundBot()
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    client = TestClient(app_module.app)
    data = {"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x", "idempotency_key": "k1"}
    assert client.post("/update-application", data=data, headers=AUTH).status_code == 404
    assert client.post("/update-application", data=data, headers=AUTH).status_code == 404
    assert bot.update_calls == 2


def test_browser_fatal_mid_job_invalidates_without_replay(app_module, monkeypatch):
    from app.scraper import BrowserFatalError

    class DyingBot(FakeBot):
        def update_application(self, **kwargs):
            self.update_calls += 1
            raise BrowserFatalError("Target closed")

    bot = DyingBot()
    invalidations = []
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    monkeypatch.setattr(app_module.session_manager, "invalidate", lambda reason="": invalidations.append(reason))
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Erreur interne du serveur"}
    assert bot.update_calls == 1
    assert invalidations == ["browser_fatal_mid_job"]


def test_degraded_session_returns_503_without_login(app_module, monkeypatch):
    from app.session_manager import SessionDegradedError

    def degraded():
        raise SessionDegradedError("login_failures")

    monkeypatch.setattr(app_module.session_manager, "get_bot", degraded)
    monkeypatch.setattr(app_module.session_manager, "is_degraded", lambda: True)
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 503
    assert "Retry-After" in response.headers


def test_session_reused_between_two_calls(app_module, monkeypatch):
    from app import session_manager as sm_module

    created = []

    class Ctor:
        def __call__(self):
            bot = FakeBot()
            bot._authenticated = False
            created.append(bot)
            return bot

    monkeypatch.setattr(sm_module.scraper_module, "TalentsoftBot", Ctor())
    manager = sm_module.SessionManager()
    monkeypatch.setattr(app_module, "session_manager", manager)
    client = TestClient(app_module.app)
    for _ in range(2):
        assert (
            client.post(
                "/update-application",
                data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": str(_)},
                headers=AUTH,
            ).status_code
            == 200
        )
    assert len(created) == 1
    assert created[0].update_calls == 2


def test_busy_returns_503_with_retry_after(app_module, fake_bot, monkeypatch):
    from app import browser_lock

    monkeypatch.setenv("BROWSER_ADMITTED_LOCK_TIMEOUT_SECONDS", "0.2")
    assert browser_lock.try_acquire(0.1)
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/applications/events",
            json={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
            headers=AUTH,
        )
    finally:
        browser_lock.release()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert fake_bot.update_calls == 0


def test_admission_queue_full_returns_503(app_module, fake_bot, monkeypatch):
    from app import browser_lock

    monkeypatch.setenv("BROWSER_MAX_QUEUED", "0")
    assert browser_lock.try_admit()
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/applications/events",
            json={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
            headers=AUTH,
        )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
    finally:
        browser_lock.release_admit()


def test_health_answers_while_job_runs(app_module, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class SlowBot(FakeBot):
        def update_application(self, **kwargs):
            started.set()
            release.wait(timeout=5)
            return super().update_application(**kwargs)

    bot = SlowBot()
    monkeypatch.setattr(app_module.session_manager, "get_bot", lambda: bot)
    client = TestClient(app_module.app)

    result = {}

    def call():
        result["response"] = client.post(
            "/applications/events",
            json={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
            headers=AUTH,
        )

    thread = threading.Thread(target=call)
    thread.start()
    assert started.wait(timeout=3)
    t0 = time.monotonic()
    health = client.get("/")
    elapsed = time.monotonic() - t0
    release.set()
    thread.join(timeout=5)
    assert health.status_code == 200
    assert health.json()["browser_busy"] is True
    assert elapsed < 0.5
    assert result["response"].status_code == 200


def test_async_mode_rejected_when_jobs_disabled(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application?async=1",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert fake_bot.update_calls == 0


def test_async_mode_enqueues_and_returns_202(app_module, fake_bot, monkeypatch):
    from app import jobs

    captured = {}

    def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return {"id": "job-1", "status": "queued"}

    monkeypatch.setattr(jobs, "is_async_jobs_enabled", lambda: True)
    monkeypatch.setattr(jobs, "enqueue_update_application", fake_enqueue)
    client = TestClient(app_module.app)
    response = client.post(
        "/update-application?async=1",
        data={"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x", "idempotency_key": "k"},
        files=[("documents", ("cv.pdf", io.BytesIO(PDF), "application/pdf"))],
        headers=AUTH,
    )
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-1", "status": "queued"}
    assert captured["candidate_email"] == "candidat@example.com"
    assert captured["offer_id"] == "25152"
    assert len(captured["document_paths"]) == 1
    assert fake_bot.update_calls == 0


def test_events_route_json(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post(
        "/applications/events",
        json={
            "candidate_email": "candidat@example.com",
            "offer_id": "25152",
            "event_type": "Entretien",
            "comment": "RAS",
            "event_date": "2026-09-12",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert fake_bot.last_kwargs["event_type"] == "Entretien"
    assert fake_bot.last_kwargs["document_paths"] == []


def test_documents_route_requires_file(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.post("/applications/documents", data={"document_category": "CV"}, headers=AUTH)
    assert response.status_code in (400, 422)


def test_list_events_read_only(app_module, fake_bot):
    client = TestClient(app_module.app)
    response = client.get(
        "/applications/events",
        params={"candidate_email": "candidat@example.com", "offer_id": "25152"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["events"] == ["evenement 1"]


def test_openapi_hidden_by_default(app_module):
    client = TestClient(app_module.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_jobs_route_hides_document_paths(app_module, monkeypatch):
    from app import jobs

    monkeypatch.setattr(
        jobs,
        "get_job",
        lambda job_id: {
            "id": job_id,
            "type": "update-application",
            "status": "queued",
            "payload": {"document_paths": ["/x"], "candidate_email": "candidat@example.com", "offer_id": "25152"},
        },
    )
    client = TestClient(app_module.app)
    body = client.get("/jobs/abc", headers=AUTH).json()
    assert "document_paths" not in body["payload"]
