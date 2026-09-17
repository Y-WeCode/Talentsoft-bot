"""Boucle de vérification d'un événement, testée sans navigateur.

Elle décide si une écriture aboutie est rapportée `verified` ou `unverified` — donc si
Hippolyte.ai enverra l'opération en revue humaine. Sa logique mérite des tests rapides et
déterministes, indépendants de Playwright.
"""

from __future__ import annotations

import pytest


def _scraper():
    """Le module courant, et non celui capture a l'import.

    La fixture `app_module` de conftest vide `sys.modules` des modules `app.*` entre les tests :
    un import de tete pointerait sur une version perimee, que les monkeypatchs ne toucheraient
    plus. Bug constate — trois tests verts isoles, rouges en suite.
    """
    from app import scraper

    return scraper


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
        from app.ts_pages import ApplicationNotOnOffer

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
    bot = object.__new__(_scraper().TalentsoftBot)
    bot.page = FakePage()
    bot._VERIFY_BUDGET_SECONDS = 0.4
    return bot


def _verify(app_page, before, signature="candidature a l'etude 14/09/2026"):
    return _scraper().TalentsoftBot._verify_event_added(_bot(), app_page, "25152", signature, before)


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
    assert app_page.reselect_calls == _scraper().TalentsoftBot._VERIFY_MAX_RESELECT_ATTEMPTS


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


# --- Vocabulaire d'erreur et drapeau de mutation --------------------------------------------
#
# `event_failed` / `upload_failed` promettent par contrat documente (docs/INTEGRATION.md) que
# l'echec precede le clic de validation, donc que le rejeu est sur. Les rendre apres un clic
# deja parti invite au doublon : c'est ce qui s'est produit en recette le 17/09 (job 7edd00fb).


class _Boom(Exception):
    """Exception quelconque, pour eprouver la branche generique."""


class FakeEventDialog:
    """Double d'EventDialog : leve la ou le test le demande."""

    raise_at: str | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def open_on_selected_application(self):
        from app.ts_pages import SelectorNotFound

        if FakeEventDialog.raise_at == "open":
            raise SelectorNotFound("formulaire introuvable")
        return object()

    def fill(self, _frame, event_type, _comment, _date):
        return event_type

    def submit(self, _frame):
        from app.ts_pages import SelectorNotFound

        if FakeEventDialog.raise_at == "submit":
            raise SelectorNotFound("bouton de validation introuvable")

    def cancel(self):
        return None


def _bot_for_event(monkeypatch, *, raise_at):
    FakeEventDialog.raise_at = raise_at
    monkeypatch.setattr(_scraper(), "EventDialog", FakeEventDialog)
    bot = _bot()
    bot._mutation_callback = None
    bot._mutation_notified = False
    bot.deadline = None
    bot.screenshot = lambda _name: None
    bot._verify_event_added = lambda *_a, **_k: True
    return bot


def test_an_interrupted_write_is_never_reported_as_replayable(monkeypatch):
    """Le bug du 17/09 : `event_failed` rendu alors que le clic etait parti.

    Apres le clic, l'appelant doit lire l'incertitude — pas une promesse de rejeu sur.
    """
    bot = _bot_for_event(monkeypatch, raise_at="submit")
    app_page = FakeApplicationPage(events=[])

    result = _scraper().TalentsoftBot.add_event(bot, app_page, "25152", "Matching positif", "texte", "2026-09-17")

    assert result["ok"] is False
    assert result["error"] == "unverified", "un clic parti ne doit jamais rendre event_failed"
    assert result["mutation_started"] is True
    assert result["mutation_may_have_happened"] is True


def test_a_failure_before_the_click_stays_replayable(monkeypatch):
    """L'autre moitie du contrat : avant le clic, `event_failed` et rejeu sur.

    Une exception anterieure a la soumission remonte volontairement : aucune ecriture n'est
    engagee, et c'est `update_application` qui qualifie l'echec via `_aborted_action`.
    """
    from app.ts_pages import SelectorNotFound

    bot = _bot_for_event(monkeypatch, raise_at="open")
    app_page = FakeApplicationPage(events=[])

    with pytest.raises(SelectorNotFound):
        _scraper().TalentsoftBot.add_event(bot, app_page, "25152", "Matching positif", "texte", "2026-09-17")

    assert bot._mutation_notified is False, "aucune ecriture n'a ete engagee"

    aborted = _scraper().TalentsoftBot._aborted_action(bot, "event_failed", event_type="Matching positif")
    assert aborted["error"] == "event_failed"
    assert "mutation_started" not in aborted
    assert "mutation_may_have_happened" not in aborted


def test_the_two_mutation_flags_can_no_longer_contradict(monkeypatch):
    """`update_details.mutation_started` et le drapeau du job partagent leur source.

    En recette ils disaient l'inverse l'un de l'autre : « rien n'a ete ecrit » cote payload,
    « une ecriture a ete engagee » cote job.
    """
    bot = _bot_for_event(monkeypatch, raise_at="submit")
    app_page = FakeApplicationPage(events=[])

    _scraper().TalentsoftBot.add_event(bot, app_page, "25152", "Matching positif", "texte", "2026-09-17")

    assert bot._mutation_notified is True
    aborted = _scraper().TalentsoftBot._aborted_action(bot, "event_failed", event_type="X")
    assert aborted["mutation_started"] is True
    assert aborted["error"] == "unverified"


def test_the_flag_is_recorded_even_without_a_callback():
    """Mode mono-processus : aucun callback n'est fourni, le bot doit quand meme le savoir."""
    bot = _bot()
    bot._mutation_callback = None
    bot._mutation_notified = False

    _scraper().TalentsoftBot._notify_mutation_started(bot)

    assert bot._mutation_notified is True


def test_a_callback_failure_does_not_lose_the_flag():
    """Une panne Redis ne doit pas faire oublier qu'une ecriture est partie."""
    bot = _bot()
    bot._mutation_notified = False
    bot._mutation_callback = lambda: (_ for _ in ()).throw(_Boom("redis down"))

    _scraper().TalentsoftBot._notify_mutation_started(bot)

    assert bot._mutation_notified is True


def test_a_dead_browser_is_fatal_whatever_its_message():
    """Reconnaitre le TYPE, pas le texte : c'est le texte qui a trompe le bot en recette."""
    from playwright._impl._errors import TargetClosedError

    is_fatal_playwright_error = _scraper().is_fatal_playwright_error

    assert is_fatal_playwright_error(TargetClosedError()) is True
    assert is_fatal_playwright_error(TargetClosedError("libelle inattendu")) is True


def test_an_ordinary_playwright_error_stays_non_fatal():
    from playwright.sync_api import Error as PlaywrightError

    is_fatal_playwright_error = _scraper().is_fatal_playwright_error

    assert is_fatal_playwright_error(PlaywrightError("locator introuvable")) is False
