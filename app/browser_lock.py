"""Mutex navigateur process-local + contrôle d'admission de la file executor.

Repris du DR bot (selenium_lock.py) : un seul job navigateur à la fois, une file
bornée derrière, 503 + Retry-After au-delà.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from fastapi import HTTPException

from . import config

_LOCK = threading.Lock()
_ADMISSION = threading.Condition()
_inflight = 0  # running + queued sur l'executor navigateur


def get_retry_after_seconds() -> int:
    return config.browser_retry_after_seconds()


def get_max_queued() -> int:
    return config.browser_max_queued()


def is_busy() -> bool:
    return _LOCK.locked()


def try_acquire(timeout_seconds: float = 2.0) -> bool:
    return _LOCK.acquire(blocking=True, timeout=timeout_seconds)


def release() -> None:
    if _LOCK.locked():
        _LOCK.release()


def busy_exception() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Browser busy",
        headers={"Retry-After": str(get_retry_after_seconds())},
    )


@contextmanager
def browser_slot(timeout_seconds: float = 2.0):
    """Prend le mutex navigateur ou lève 503 + Retry-After."""
    if not try_acquire(timeout_seconds):
        raise busy_exception()
    try:
        yield
    finally:
        release()


def try_admit() -> bool:
    """Réserve une place dans la file (1 en cours + max_queued en attente)."""
    global _inflight
    with _ADMISSION:
        if _inflight >= 1 + get_max_queued():
            return False
        _inflight += 1
        return True


def release_admit() -> None:
    global _inflight
    with _ADMISSION:
        if _inflight > 0:
            _inflight -= 1
        _ADMISSION.notify_all()


def inflight_count() -> int:
    with _ADMISSION:
        return _inflight
