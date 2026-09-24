"""Worker : routage des types de job, codes d'erreur, et anti-rejeu.

Aucun thread, aucun processus, aucun navigateur : `pump_worker` exécute le job comme le ferait
la boucle du worker, sur un Redis en mémoire.

Les modules `app.*` sont importés **dans** les fixtures et non en tête de fichier : `app_module`
les recharge à chaque test pour isoler la configuration, et un import de tête pointerait donc sur
une version périmée.
"""

from __future__ import annotations

import pytest
from conftest import FakeBot, pump_worker


@pytest.fixture
def ts(app_module, fake_redis, monkeypatch):
    """Outillage du test : modules frais, bot double interchangeable, et empilage."""
    from types import SimpleNamespace

    from app import idempotency, jobs
    from app import session_manager as sm_module

    def install(bot):
        monkeypatch.setattr(sm_module.session_manager, "get_bot", lambda: bot)
        monkeypatch.setattr(sm_module.session_manager, "invalidate", lambda *a, **k: None)
        return bot

    def refuse_login(error):
        def raise_it():
            raise error

        monkeypatch.setattr(sm_module.session_manager, "get_bot", raise_it)
        monkeypatch.setattr(sm_module.session_manager, "invalidate", lambda *a, **k: None)

    def push(payload=None, job_type=jobs.JOB_TYPE_UPDATE_APPLICATION, key=None):
        payload = payload or {"candidate_email": "candidat@example.com", "offer_id": "25152", "comment": "x"}
        if key:
            idempotency.reserve(key)
        return jobs.enqueue_job(job_type, payload, key)

    return SimpleNamespace(
        jobs=jobs,
        idempotency=idempotency,
        redis=fake_redis,
        install=install,
        refuse_login=refuse_login,
        push=push,
    )


# --- Chemin nominal -------------------------------------------------------------------------


def test_update_application_completes_and_memorises_its_result(ts):
    ts.install(FakeBot())
    done = pump_worker(ts.push(key="k1")["id"])

    assert done["status"] == "completed"
    assert done["result"]["success"] is True
    # Le resultat est memorise : un rejeu sous la meme cle rend la reponse, sans reecrire.
    state, replay = ts.idempotency.reserve("k1")
    assert state == "replay"
    assert replay == done["result"]


def test_reads_are_served_before_pushes(ts):
    ts.push()
    ts.push(job_type=ts.jobs.JOB_TYPE_LIST_EVENTS)
    assert ts.redis.llen(ts.jobs.QUEUE_PUSH) == 1
    assert ts.redis.llen(ts.jobs.QUEUE_READ) == 1
    # La lecture passe en premier, bien qu'empilee apres la mutation.
    assert ts.jobs.brpop_next_job(timeout_seconds=0)[0] == ts.jobs.QUEUE_READ


def test_list_events_job_returns_the_history(ts):
    ts.install(FakeBot())
    job = ts.push({"candidate_email": "candidat@example.com", "offer_id": "25152"}, ts.jobs.JOB_TYPE_LIST_EVENTS)
    done = pump_worker(job["id"])

    assert done["status"] == "completed"
    assert done["result"] == {"offer_id": "25152", "events": ["evenement 1"]}
    assert done.get("mutation_started") is not True


# --- Échecs ---------------------------------------------------------------------------------


def test_login_failure_leaves_the_job_replayable(ts):
    """Un échec de login n'a rien écrit : la clé d'idempotence doit être libérée."""
    from app.session_manager import SessionBootstrapError

    ts.refuse_login(SessionBootstrapError("LoginError: credentials_rejected"))
    done = pump_worker(ts.push(key="k2")["id"])

    assert done["status"] == "failed"
    assert done["error_code"] == "session_bootstrap_failed"
    assert done.get("mutation_started") is not True
    assert ts.idempotency.reserve("k2")[0] == "reserved"


def test_failure_after_the_first_write_keeps_the_job_locked(ts):
    """Une écriture engagée puis perdue : ni rejeu, ni libération de la clé."""
    from app.scraper import BrowserFatalError

    class HalfWayBot(FakeBot):
        def update_application(self, **kwargs):
            kwargs["on_mutation_started"]()
            raise BrowserFatalError("Target closed")

    ts.install(HalfWayBot())
    job = ts.push(key="k3")
    done = pump_worker(job["id"])

    assert done["status"] == "failed"
    assert done["error_code"] == "browser_fatal"
    assert done["mutation_started"] is True
    assert ts.idempotency.reserve("k3")[0] == "in_progress"
    # Rejouer ce job est refuse, meme s'il revenait dans la file.
    assert pump_worker(job["id"])["error_code"] == "mutation_started_no_rejeu"


def test_browser_fatal_never_leaks_the_playwright_message(ts):
    from app.scraper import BrowserFatalError

    class DyingBot(FakeBot):
        def update_application(self, **kwargs):
            raise BrowserFatalError("Target closed at https://tenant.talent-soft.com/x?token=abc")

    ts.install(DyingBot())
    done = pump_worker(ts.push()["id"])

    assert done["error_code"] == "browser_fatal"
    assert done["error_detail"] == "BrowserFatalError"
    assert "token=abc" not in str(done)


def test_third_party_exception_keeps_only_its_type(ts):
    class OddBot(FakeBot):
        def update_application(self, **kwargs):
            raise ValueError("mot de passe: s3cr3t")

    ts.install(OddBot())
    done = pump_worker(ts.push()["id"])

    assert done["error_code"] == "internal_error"
    assert done["error_detail"] == "ValueError"
    assert "s3cr3t" not in str(done)


def test_candidate_not_found_keeps_its_own_code(ts):
    from app.ts_pages import CandidateNotFound

    class EmptyBot(FakeBot):
        def update_application(self, **kwargs):
            raise CandidateNotFound("introuvable")

    ts.install(EmptyBot())
    assert pump_worker(ts.push()["id"])["error_code"] == "candidate_not_found"


def test_unknown_job_type_fails_without_touching_the_browser(ts):
    bot = ts.install(FakeBot())
    done = pump_worker(ts.jobs.enqueue_job("type-invente", {})["id"])

    assert done["status"] == "failed"
    assert done["error_code"] == "unknown_job_type"
    assert bot.update_calls == 0


def test_unverified_action_is_reported_as_possibly_mutated(ts):
    """Écriture non vérifiable : l'appelant doit contrôler avant de rejouer."""
    ts.install(FakeBot(actions={"event": {"ok": False, "error": "unverified", "mutation_may_have_happened": True}}))
    done = pump_worker(ts.push()["id"])

    assert done["status"] == "completed"
    assert done["result"]["success"] is False
    assert done["mutation_may_have_happened"] is True


# --- Idempotence d'un echec rejouable (issue #17) --------------------------------------------
#
# La note de migration garantit qu'un `event_failed` / `upload_failed` est rejouable en l'etat.
# Memoriser le resultat d'un job `completed` mais infructueux neutralisait cette promesse : la
# re-soumission rendait le resultat memorise, sans rien reexecuter, pendant tout le TTL.


def test_a_failure_without_any_write_stays_replayable(ts):
    """La reproduction de l'issue : deuxieme soumission, meme cle, le travail doit repartir."""
    # `event_failed` garantit qu'aucune ecriture n'a ete engagee : le payload le dit aussi.
    ts.install(FakeBot(actions={"event": {"ok": False, "error": "event_failed"}}, mutation_started=False))
    done = pump_worker(ts.push(key="k-rejouable")["id"])

    assert done["status"] == "completed"
    assert done["result"]["success"] is False
    # La cle est libre : un second appel refera le travail au lieu de rendre cet echec.
    assert ts.idempotency.reserve("k-rejouable")[0] == "reserved"


def test_a_successful_job_is_still_memorised(ts):
    """L'idempotence garde tout son role sur ce qui a abouti."""
    ts.install(FakeBot())
    done = pump_worker(ts.push(key="k-succes")["id"])

    assert done["result"]["success"] is True
    state, replay = ts.idempotency.reserve("k-succes")
    assert state == "replay"
    assert replay == done["result"]


def test_a_partial_success_is_memorised_not_released(ts):
    """Cas partiel : l'evenement est ecrit, le document echoue.

    Le succes global est faux, mais une ecriture a bien eu lieu — liberer la cle rouvrirait la
    porte au doublon que l'idempotence existe pour empecher.
    """
    ts.install(
        FakeBot(
            actions={
                "event": {"ok": True, "verified": True, "mutation_started": True},
                "documents": [{"ok": False, "error": "upload_failed"}],
            },
            mutation_started=True,
        )
    )
    done = pump_worker(ts.push(key="k-partiel")["id"])

    assert done["result"]["success"] is False
    assert ts.idempotency.reserve("k-partiel")[0] == "replay"


def test_an_unverified_write_is_memorised(ts):
    """Ecriture peut-etre aboutie : surtout ne pas liberer la cle."""
    ts.install(
        FakeBot(
            actions={"event": {"ok": False, "error": "unverified", "mutation_may_have_happened": True}},
            mutation_started=True,
        )
    )
    done = pump_worker(ts.push(key="k-incertain")["id"])

    assert done["result"]["success"] is False
    assert ts.idempotency.reserve("k-incertain")[0] == "replay"


# --- Worker suspendu et jobs orphelins --------------------------------------------------------
#
# Les delais de Playwright sont appliques PAR SON PILOTE. Quand celui-ci meurt, l'appel Python
# reste suspendu sans exception ni timeout. Constate en recette : six jours sur le meme job,
# quinze jobs empiles derriere, et un battement de coeur qui annoncait un worker en bonne sante.


def _worker():
    from app import worker as worker_module

    return worker_module


def test_a_job_left_running_is_concluded_at_startup(ts):
    """Sans cette reprise, le job reste « en cours » jusqu'a son TTL de 24 h."""
    job = ts.push(key="k-orphelin")
    job["status"] = "running"
    ts.jobs.save_job(job)

    _worker()._recover_orphan_jobs()

    recovered = ts.jobs.get_job(job["id"])
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "worker_interrupted"
    # Rien n'avait ete ecrit : la cle redevient disponible.
    assert ts.idempotency.reserve("k-orphelin")[0] == "reserved"


def test_an_orphan_that_had_written_keeps_its_key(ts):
    """Une ecriture engagee puis perdue ne se rejoue pas : la cle reste prise."""
    job = ts.push(key="k-orphelin-ecrit")
    job["status"] = "running"
    job["mutation_started"] = True
    ts.jobs.save_job(job)

    _worker()._recover_orphan_jobs()

    recovered = ts.jobs.get_job(job["id"])
    assert recovered["error_code"] == "worker_interrupted"
    assert recovered["mutation_started"] is True
    assert ts.idempotency.reserve("k-orphelin-ecrit")[0] == "in_progress"


def test_a_finished_job_is_left_alone(ts):
    """La reprise ne doit toucher qu'aux jobs restes en cours."""
    ts.install(FakeBot())
    done_job = pump_worker(ts.push(key="k-fini")["id"])

    _worker()._recover_orphan_jobs()

    assert ts.jobs.get_job(done_job["id"])["status"] == "completed"


def test_the_watchdog_leaves_a_job_within_its_budget_alone(ts, monkeypatch):
    worker_module = _worker()
    exits = []
    monkeypatch.setattr(worker_module.os, "_exit", lambda code: exits.append(code))
    job = ts.push()
    worker_module._set_current_job(job["id"])

    worker_module._abort_if_stuck()

    assert exits == [], "un job qui vient de demarrer ne doit pas tuer le worker"
    worker_module._set_current_job(None)


def test_the_watchdog_concludes_the_job_and_exits_when_stuck(ts, monkeypatch):
    """Le processus est irrecuperable : on conclut le job, puis on sort."""
    worker_module = _worker()
    exits = []
    monkeypatch.setattr(worker_module.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(worker_module, "_hard_budget_seconds", lambda: 0)

    job = ts.push(key="k-bloque")
    job["status"] = "running"
    ts.jobs.save_job(job)
    worker_module._set_current_job(job["id"])
    try:
        worker_module._abort_if_stuck()
    finally:
        worker_module._set_current_job(None)

    assert exits == [1]
    concluded = ts.jobs.get_job(job["id"])
    assert concluded["status"] == "failed"
    assert concluded["error_code"] == "worker_stuck"


def test_the_heartbeat_exposes_how_long_the_job_has_been_running(ts):
    """Un battement qui dit seulement « vivant » a laisse passer six jours de blocage."""
    worker_module = _worker()
    job = ts.push()
    worker_module._set_current_job(job["id"])
    try:
        payload = worker_module._heartbeat_payload()
    finally:
        worker_module._set_current_job(None)

    assert payload["current_job_id"] == job["id"]
    assert payload["current_job_seconds"] >= 0
    assert worker_module._heartbeat_payload()["current_job_id"] is None
