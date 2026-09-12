"""Garde-fous : URLs limitées au tenant, uploads sûrs, contrat d'actions."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException

from . import config

DEFAULT_MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024
DEFAULT_ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".png", ".jpg", ".jpeg"}

# Octets magiques par extension. Une extension absente ici n'est pas contrôlée (txt).
MAGIC_BYTES_BY_EXTENSION: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".odt": (b"PK\x03\x04",),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".rtf": (b"{\\rtf",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}

APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def get_max_upload_size_bytes() -> int:
    return config.env_int("MAX_UPLOAD_SIZE_BYTES", DEFAULT_MAX_UPLOAD_SIZE_BYTES, minimum=1)


def get_allowed_upload_extensions() -> set[str]:
    raw_value = os.getenv("UPLOAD_ALLOWED_EXTENSIONS")
    if not raw_value:
        return DEFAULT_ALLOWED_UPLOAD_EXTENSIONS
    extensions = set()
    for item in raw_value.split(","):
        extension = item.strip().lower()
        if not extension:
            continue
        extensions.add(extension if extension.startswith(".") else f".{extension}")
    return extensions or DEFAULT_ALLOWED_UPLOAD_EXTENSIONS


def expected_host() -> str:
    return urlparse(config.ts_base_url()).netloc.lower()


def is_same_origin(url: str) -> bool:
    parsed = urlparse(str(url))
    return parsed.scheme == "https" and parsed.netloc.lower() == expected_host() and bool(expected_host())


def validate_talentsoft_url(value: str) -> str:
    """N'accepte qu'une URL HTTPS sur l'hôte configuré par TS_BASE_URL."""
    if not is_same_origin(value):
        raise HTTPException(status_code=400, detail="URL Talentsoft invalide")
    return str(value)


def validate_application_id(value: str) -> str:
    value = (value or "").strip()
    if not APPLICATION_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="application_id invalide")
    return value


def build_application_url(application_id: str) -> str:
    template = config.ts_application_url_template()
    if "{application_id}" not in template:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")
    url = template.format(base=config.ts_base_url(), application_id=validate_application_id(application_id))
    return validate_talentsoft_url(url)


def resolve_application_url(application_id: str | None, application_url: str | None) -> tuple[str, str]:
    """Retourne (application_id, url). L'URL explicite prime si elle est sur le tenant."""
    if application_url:
        url = validate_talentsoft_url(application_url)
        app_id = validate_application_id(application_id) if application_id else _id_from_url(url)
        return app_id, url
    if not application_id:
        raise HTTPException(status_code=400, detail="application_id ou application_url requis")
    app_id = validate_application_id(application_id)
    return app_id, build_application_url(app_id)


def _id_from_url(url: str) -> str:
    segments = [s for s in urlparse(url).path.split("/") if s]
    for segment in reversed(segments):
        if APPLICATION_ID_PATTERN.match(segment):
            return segment
    return "unknown"


def sanitize_comment(comment: str | None) -> str | None:
    if comment is None:
        return None
    normalized = comment.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    if len(normalized) > config.comment_max_chars():
        raise HTTPException(status_code=400, detail="Commentaire trop long")
    return normalized


def build_safe_upload_path(original_filename: str) -> str:
    """Chemin d'upload appartenant au serveur, impossible à sortir de UPLOAD_DIR."""
    extension = Path(original_filename or "").suffix.lower()
    if extension not in get_allowed_upload_extensions():
        raise HTTPException(status_code=400, detail="Extension de fichier non autorisée")

    upload_root = Path(config.UPLOAD_DIR).resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    upload_path = (upload_root / f"{uuid.uuid4().hex}{extension}").resolve()
    try:
        upload_path.relative_to(upload_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide") from exc
    return str(upload_path)


def verify_magic_bytes(path: str) -> None:
    """Refuse un fichier dont le contenu ne correspond pas à son extension."""
    extension = Path(path).suffix.lower()
    signatures = MAGIC_BYTES_BY_EXTENSION.get(extension)
    if not signatures:
        return
    with open(path, "rb") as handle:
        head = handle.read(16)
    if not any(head.startswith(sig) for sig in signatures):
        raise HTTPException(status_code=400, detail="Contenu du fichier incohérent avec son extension")


def safe_display_filename(filename: str | None, fallback_extension: str = "") -> str:
    """Nom affiché côté Talentsoft : caractères sûrs uniquement, longueur bornée."""
    stem = Path(filename or "").stem
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._") or "document"
    extension = Path(filename or "").suffix.lower() or fallback_extension
    return f"{cleaned[:80]}{extension}"


def short_hash(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:16]


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def actions_succeeded(actions: dict) -> bool:
    """True si chaque action a ok=True (skipped compte comme ok). Les listes sont parcourues."""
    if not actions:
        return True
    for result in actions.values():
        if isinstance(result, list):
            if not all(isinstance(item, dict) and item.get("ok") is True for item in result):
                return False
        elif not (isinstance(result, dict) and result.get("ok") is True):
            return False
    return True
