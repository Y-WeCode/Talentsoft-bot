import ast
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_BASE_URL", "https://tenant.talent-soft.com")
    monkeypatch.delenv("TS_AUTH_HOSTS", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app import idempotency

    idempotency.reset_memory_store()


def test_offer_reference_matching_is_strict(env):
    """L'appariement d'une candidature repose sur la reference de l'offre : il doit etre strict."""
    from app import safety

    row = "Reponse a offre Agent d'Escale Commercial F/H ( ref. 2026-25152)"
    assert safety.offer_reference_matches(row, "25152")
    assert not safety.offer_reference_matches(row, "23770")
    # Un identifiant plus court ne doit pas matcher par simple inclusion.
    assert not safety.offer_reference_matches(row, "152")
    # Ni un identifiant prefixe d'un numero plus long.
    assert not safety.offer_reference_matches("... ( ref. 2026-251521)", "25152")
    assert not safety.offer_reference_matches(row, "")


def test_auth_hosts_are_allowed_but_nothing_else(env, monkeypatch):
    """L'allowlist d'authentification ne doit pas ouvrir la navigation au-dela."""
    from app import safety

    monkeypatch.setenv("TS_AUTH_HOSTS", "idp.talent-soft.com, fed.talent-soft.com")
    assert safety.is_allowed_navigation("https://tenant.talent-soft.com/Pages/x.aspx")
    assert safety.is_allowed_navigation("https://idp.talent-soft.com/wsfed/issue")
    assert safety.is_allowed_navigation("https://fed.talent-soft.com/choose")
    assert not safety.is_allowed_navigation("https://evil.example.com/")
    # Le protocole reste impose.
    assert not safety.is_allowed_navigation("http://idp.talent-soft.com/wsfed/issue")


def test_application_id_validation(env):
    from app import safety

    for bad in ("", "../x", "a b", "x" * 65, "1;drop"):
        with pytest.raises(HTTPException) as exc:
            safety.validate_application_id(bad)
        assert exc.value.status_code == 400


def test_foreign_url_rejected_even_with_similar_host(env):
    from app import safety

    for url in (
        "https://tenant.talent-soft.com.evil.com/x",
        "http://tenant.talent-soft.com/x",
        "https://evil.com/?u=tenant.talent-soft.com",
    ):
        with pytest.raises(HTTPException):
            safety.validate_talentsoft_url(url)
    assert safety.is_same_origin("https://tenant.talent-soft.com/anything") is True


def test_magic_bytes(env, tmp_path):
    from app import safety

    good = tmp_path / "ok.pdf"
    good.write_bytes(b"%PDF-1.7 ...")
    safety.verify_magic_bytes(str(good))

    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(HTTPException):
        safety.verify_magic_bytes(str(bad))

    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"anything goes")
    safety.verify_magic_bytes(str(txt))


def test_safe_display_filename(env):
    from app import safety

    assert safety.safe_display_filename("../../Synthèse candidat.PDF") == "Synth_se candidat.pdf"
    assert safety.safe_display_filename(None, ".pdf") == "document.pdf"


def test_sanitize_comment(env, monkeypatch):
    from app import safety

    assert safety.sanitize_comment("  a\r\nb  ") == "a\nb"
    assert safety.sanitize_comment("   ") is None
    monkeypatch.setenv("COMMENT_MAX_CHARS", "3")
    with pytest.raises(HTTPException):
        safety.sanitize_comment("abcd")


def test_actions_succeeded_handles_lists(env):
    from app import safety

    assert safety.actions_succeeded({}) is True
    assert safety.actions_succeeded({"event": {"ok": True}, "documents": [{"ok": True, "skipped": True}]}) is True
    assert safety.actions_succeeded({"documents": [{"ok": True}, {"ok": False}]}) is False
    assert safety.actions_succeeded({"event": {"ok": False}}) is False
    assert safety.actions_succeeded({"event": "weird"}) is False


def test_idempotency_lifecycle(env):
    from app import idempotency

    assert idempotency.reserve("k") == ("reserved", None)
    assert idempotency.reserve("k") == ("in_progress", None)
    idempotency.store_result("k", {"success": True})
    assert idempotency.reserve("k") == ("replay", {"success": True})
    idempotency.release("k")
    assert idempotency.reserve("k") == ("reserved", None)


def test_idempotency_memory_ttl(env, monkeypatch):
    from app import idempotency

    monkeypatch.setenv("IDEMPOTENCY_TTL_SECONDS", "1")
    assert idempotency.reserve("t") == ("reserved", None)
    monkeypatch.setattr(idempotency.time, "time", lambda: 1e12)
    assert idempotency.reserve("t") == ("reserved", None)


def test_no_sleep_ge_one_second_in_browser_modules():
    for filename in ("scraper.py", "ts_pages.py", "session_manager.py"):
        source = (Path(__file__).resolve().parents[1] / "app" / filename).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                func = node.func
                is_sleep = (isinstance(func, ast.Attribute) and func.attr == "sleep") or (
                    isinstance(func, ast.Name) and func.id == "sleep"
                )
                if not is_sleep or not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                    assert arg.value < 1, f"time.sleep({arg.value}) interdit dans {filename}"


def test_no_secret_logging_in_scraper():
    """Les mots de passe et les cookies ne doivent jamais être interpolés dans un log."""
    source = (Path(__file__).resolve().parents[1] / "app" / "scraper.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "logger." in line:
            assert "password" not in line.lower() or "mismatch" in line.lower()
            assert "cookie" not in line.lower()
