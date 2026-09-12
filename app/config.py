"""Configuration centralisée (variables d'environnement).

Toutes les valeurs sont lues à la demande pour rester testables (monkeypatch env).
Les identifiants Talentsoft ne sont jamais journalisés.
"""

from __future__ import annotations

import os


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Talentsoft -----------------------------------------------------------------


def ts_base_url() -> str:
    return env_str("TS_BASE_URL", "").rstrip("/")


def ts_username() -> str:
    return env_str("TS_USERNAME", "")


def ts_password() -> str:
    return env_str("TS_PASSWORD", "")


def ts_application_url_template() -> str:
    # Gabarit à confirmer en phase 0 (docs/DISCOVERY.md). Doit contenir {application_id}.
    return env_str("TS_APPLICATION_URL_TEMPLATE", "{base}/Recruiting/BackOffice/Applications/{application_id}")


def ts_default_event_type() -> str:
    return env_str("TS_DEFAULT_EVENT_TYPE", "")


def ts_default_document_category() -> str:
    return env_str("TS_DEFAULT_DOCUMENT_CATEGORY", "")


def ts_selftest_application_id() -> str:
    return env_str("TS_SELFTEST_APPLICATION_ID", "")


# --- API ---------------------------------------------------------------------------


def api_tokens() -> list[str]:
    """Token courant + token précédent (rotation sans coupure)."""
    tokens = [env_str("API_TOKEN", ""), env_str("API_TOKEN_PREVIOUS", "")]
    return [t for t in tokens if t]


def enable_api_docs() -> bool:
    return env_bool("ENABLE_API_DOCS", False)


def comment_max_chars() -> int:
    return env_int("COMMENT_MAX_CHARS", 4000, minimum=1)


def idempotency_ttl_seconds() -> int:
    return env_int("IDEMPOTENCY_TTL_SECONDS", 86400, minimum=1)


# --- Navigateur --------------------------------------------------------------------


def headless_mode() -> bool:
    return env_bool("HEADLESS_MODE", True)


def browser_executable_path() -> str:
    return env_str("BROWSER_EXECUTABLE_PATH", "")


def action_timeout_ms() -> int:
    return env_int("ACTION_TIMEOUT_MS", 30000, minimum=1000)


def navigation_timeout_ms() -> int:
    return env_int("NAVIGATION_TIMEOUT_MS", 45000, minimum=1000)


def job_timeout_seconds() -> int:
    return env_int("JOB_TIMEOUT_SECONDS", 300, minimum=10)


def browser_retry_after_seconds() -> int:
    return env_int("BROWSER_RETRY_AFTER_SECONDS", 60, minimum=1)


def browser_max_queued() -> int:
    return env_int("BROWSER_MAX_QUEUED", 2, minimum=0)


def browser_admitted_lock_timeout_seconds() -> float:
    return env_float("BROWSER_ADMITTED_LOCK_TIMEOUT_SECONDS", 1800.0)


def session_idle_ttl_seconds() -> int:
    return env_int("BROWSER_SESSION_IDLE_TTL_SECONDS", 1800, minimum=1)


def session_max_age_seconds() -> int:
    return env_int("BROWSER_SESSION_MAX_AGE_SECONDS", 7200, minimum=1)


def login_max_failures() -> int:
    return env_int("LOGIN_MAX_FAILURES", 3, minimum=1)


def login_failure_window_seconds() -> int:
    return env_int("LOGIN_FAILURE_WINDOW_SECONDS", 600, minimum=1)


# --- Observabilité -----------------------------------------------------------------


def traces_enabled() -> bool:
    return env_bool("TRACES_ENABLED", False)


def screenshots_enabled() -> bool:
    return env_bool("SCREENSHOTS_ENABLED", False)


def traces_retention_days() -> int:
    return env_int("TRACES_RETENTION_DAYS", 7, minimum=0)


# --- Dossiers -----------------------------------------------------------------------

STATE_DIR = "state"
UPLOAD_DIR = "uploads"
TRACES_DIR = "traces"
LOGS_DIR = "logs"
DATA_DIR = "data"


def storage_state_path() -> str:
    return os.path.join(STATE_DIR, "storage_state.json")


# --- Jobs async ---------------------------------------------------------------------


def redis_url() -> str:
    return env_str("REDIS_URL", "")


def async_jobs_enabled() -> bool:
    return env_bool("TS_ASYNC_JOBS_ENABLED", False) and bool(redis_url())
