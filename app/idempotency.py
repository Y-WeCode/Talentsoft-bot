"""Dédoublonnage des mutations, en mode sync comme async.

Un second appel portant la même clé renvoie le résultat mémorisé au lieu de rejouer
la mutation dans Talentsoft. Redis si REDIS_URL est défini, sinon mémoire process.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config

KEY_PREFIX = "ts:idem:"
IN_PROGRESS = "__in_progress__"


class _MemoryStore:
    def __init__(self):
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._data.items() if exp <= now]
        for key in expired:
            self._data.pop(key, None)

    def get(self, key: str) -> str | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            item = self._data.get(key)
            return item[1] if item else None

    def set_nx(self, key: str, value: str, ttl: int) -> bool:
        now = time.time()
        with self._lock:
            self._purge(now)
            if key in self._data:
                return False
            self._data[key] = (now + ttl, value)
            return True

    def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class _RedisStore:
    def __init__(self, url: str):
        import redis

        self._r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._r.get(key)

    def set_nx(self, key: str, value: str, ttl: int) -> bool:
        return bool(self._r.set(key, value, ex=ttl, nx=True))

    def set(self, key: str, value: str, ttl: int) -> None:
        self._r.set(key, value, ex=ttl)

    def delete(self, key: str) -> None:
        self._r.delete(key)


_memory = _MemoryStore()


def _store():
    url = config.redis_url()
    if url:
        try:
            return _RedisStore(url)
        except Exception:  # pragma: no cover - repli défensif
            return _memory
    return _memory


def reserve(key: str) -> tuple[str, dict[str, Any] | None]:
    """Tente de réserver la clé.

    Retourne ("reserved", None) si la mutation peut démarrer,
    ("in_progress", None) si un autre appel identique est en cours,
    ("replay", payload) si un résultat est déjà mémorisé.
    """
    full_key = f"{KEY_PREFIX}{key}"
    store = _store()
    if store.set_nx(full_key, IN_PROGRESS, config.idempotency_ttl_seconds()):
        return "reserved", None
    existing = store.get(full_key)
    if existing is None:
        if store.set_nx(full_key, IN_PROGRESS, config.idempotency_ttl_seconds()):
            return "reserved", None
        return "in_progress", None
    if existing == IN_PROGRESS:
        return "in_progress", None
    try:
        return "replay", json.loads(existing)
    except json.JSONDecodeError:
        store.delete(full_key)
        if store.set_nx(full_key, IN_PROGRESS, config.idempotency_ttl_seconds()):
            return "reserved", None
        return "in_progress", None


def store_result(key: str, payload: dict[str, Any]) -> None:
    _store().set(f"{KEY_PREFIX}{key}", json.dumps(payload), config.idempotency_ttl_seconds())


def release(key: str) -> None:
    """Libère une réservation quand la mutation n'a pas démarré (échec amont)."""
    _store().delete(f"{KEY_PREFIX}{key}")


def reset_memory_store() -> None:
    """Pour les tests."""
    global _memory
    _memory = _MemoryStore()
