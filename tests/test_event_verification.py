"""Boucle de vérification d'un événement, testée sans navigateur.

Elle décide si une écriture aboutie est rapportée `verified` ou `unverified` — donc si
Hippolyte.ai enverra l'opération en revue humaine. Sa logique mérite des tests rapides et
déterministes, indépendants de Playwright.
"""

from __future__ import annotations

from app.scraper import TalentsoftBot
from app.ts_pages import ApplicationNotOnOffer


class FakePage:
    def wait_for_timeout(self, _ms):  # la boucle ne doit pas dormir pendant les tests
        return None


class FakeApplicationPage:
    """Double d'ApplicationPage : la resélection peut échouer, la lecture répondre quand même."""

    def __init__(self, *, events, reselect_ok=True, shape=None):
        self._events = events
        self._reselect_ok = reselect_ok
        self.reselect_calls = 0
        self.list_calls = 0
        self._shape = shape or {
            "table": True,
            "applications": 1,
            "events": len(events),
            "other": 1,
            "selected_line": True,
        }

    def select_application_by_offer(self, offer_id):
        self.reselect_calls += 1
        if not self._reselect_ok:
            raise ApplicationNotOnOffer(f"offre {offer_id} absente de la fiche")
        return "reponse a offre ( ref. 2026-25152 )"

    def list_events(self, _offer_id=None):
        self.list_calls += 1
        return list(self._events)

    def history_shape(self):
        return dict(self._shape)

    def application_row_texts(self):
        return ["reponse a offre technicien ( ref. 2026-25152 )"]


def _bot():
    """Un bot sans navigateur : `__init__` lancerait Chromium.

    Budget raccourci : la boucle s'appuie sur l'horloge murale, et la valeur de production
    ferait attendre quinze secondes chaque test d'echec.
    """
    bot = object.__new__(TalentsoftBot)
    bot.page = FakePage()
    bot._VERIFY_BUDGET_SECONDS = 0.4
    return bot


def _verify(app_page, before, signature="candidature a l'etude 14/09/2026"):
    return TalentsoftBot._verify_event_added(_bot(), app_page, "25152", signature, before)


def test_a_failing_reselection_never_starves_the_reading():
    """La régression : la lecture ne doit pas dépendre du succès de la resélection.

    Une resélection systématiquement en échec faisait expirer les 15 s sans qu'une seule lecture
    soit tentée, et le bot rapportait `unverified` sur une écriture pourtant aboutie.
    """
    app_page = FakeApplicationPage(
        events=["candidature a l'etude 14/09/2026 recruteur", "autre 12/09/2026 recruteur"],
        reselect_ok=False,
    )

    assert _verify(app_page, before=1) is True
    assert app_page.list_calls >= 1, "aucune lecture n'a été tentée"


def test_reselection_is_attempted_but_bounded():
    """Deux tentatives au plus : au-delà, tout le budget restant revient à la lecture."""
    app_page = FakeApplicationPage(events=["rien 01/01/2026"], reselect_ok=False)

    assert _verify(app_page, before=1) is False
    assert app_page.reselect_calls == TalentsoftBot._VERIFY_MAX_RESELECT_ATTEMPTS


def test_a_successful_reselection_is_enough_to_verify():
    app_page = FakeApplicationPage(events=["candidature a l'etude 14/09/2026 recruteur"], reselect_ok=True)

    assert _verify(app_page, before=0) is True
    assert app_page.reselect_calls == 1


def test_an_unchanged_count_stays_unverified():
    """La signature seule ne suffit pas : un événement préexistant de même type et même date
    validerait une écriture qui n'a pas eu lieu."""
    app_page = FakeApplicationPage(events=["candidature a l'etude 14/09/2026 recruteur"], reselect_ok=True)

    assert _verify(app_page, before=1) is False


def test_the_failure_reason_reflects_what_was_actually_measured(caplog):
    """`reason` ne doit jamais désigner une cause qu'on n'a pas constatée."""
    app_page = FakeApplicationPage(events=["rien 01/01/2026"], reselect_ok=False)

    with caplog.at_level("WARNING"):
        assert _verify(app_page, before=1) is False

    line = next(m for m in caplog.messages if m.startswith("verify_failed "))
    assert "reason=reselect_failed" in line
    assert "reselect_ok=0" in line
    assert "rows_after=1" in line, "le nombre de lignes lues doit être réel, pas la valeur initiale"


def test_event_row_text_never_reaches_the_logs(caplog):
    """Les lignes d'événement portent un type et un auteur, donc une personne : seules les
    lignes de candidature, qui portent une offre, peuvent sortir."""
    app_page = FakeApplicationPage(events=["convocation 14/09/2026 Jean Dupont"], reselect_ok=False)

    with caplog.at_level("WARNING"):
        _verify(app_page, before=5)

    assert not any("Jean Dupont" in message for message in caplog.messages)
    assert any("verify_failed_applications" in message for message in caplog.messages)


def test_a_missing_table_is_named_as_such(caplog):
    app_page = FakeApplicationPage(
        events=[],
        reselect_ok=False,
        shape={"table": False, "applications": 0, "events": 0, "other": 0, "selected_line": False},
    )

    with caplog.at_level("WARNING"):
        assert _verify(app_page, before=1) is False

    assert any("reason=table_missing" in message for message in caplog.messages)
