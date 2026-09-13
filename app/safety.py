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

# Limite affichée par le formulaire de dépôt du Back Office : 10240 Ko.
DEFAULT_MAX_UPLOAD_SIZE_BYTES = 10240 * 1024

# Extensions réellement acceptées par Talentsoft (relevé sur le tenant, docs/DISCOVERY.md) :
# « les types de documents autorisés sont : .doc .rtf .docx .pdf .tif .tiff .xlsx .zip ».
# Ni .odt, ni .txt, ni .png, ni .jpg : un fichier de ce type serait accepté par le bot puis
# rejeté par Talentsoft, et le push échouerait après le début de la mutation.
DEFAULT_ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf", ".tif", ".tiff", ".xlsx", ".zip"}

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
OFFER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Volontairement permissif : on ne cherche pas à valider la RFC 5322, seulement à écarter
# ce qui ne peut pas être un email et ne doit pas partir dans une recherche Back Office.
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,128}\.[A-Za-z]{2,24}$")


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


def is_allowed_navigation(url: str) -> bool:
    """Tenant, ou l'un des hôtes du parcours d'authentification fédérée.

    Le login traverse une passerelle de fédération et un IdP hébergés sur d'autres domaines
    (docs/DISCOVERY.md) : sans cette tolérance, la connexion est impossible. Tout le reste
    demeure bloqué, pour que les cookies de session ne quittent pas ces hôtes.
    """
    if is_same_origin(url):
        return True
    parsed = urlparse(str(url))
    if parsed.scheme != "https":
        return False
    host = parsed.netloc.lower()
    return bool(host) and host in set(config.ts_auth_hosts())


def validate_talentsoft_url(value: str) -> str:
    """N'accepte qu'une URL HTTPS sur l'hôte configuré par TS_BASE_URL."""
    if not is_same_origin(value):
        raise HTTPException(status_code=400, detail="URL Talentsoft invalide")
    return str(value)


def validate_application_id(value: str) -> str:
    """Valide un identifiant applicatif Talentsoft.

    Plus utilise par les routes : une candidature est designee par (email, offre), le Back
    Office n'acceptant pas les identifiants de l'API Recruiting Customer (docs/DISCOVERY.md).
    Conserve pour valider un identifiant recu d'un appelant tiers.
    """
    value = (value or "").strip()
    if not APPLICATION_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="application_id invalide")
    return value


def validate_offer_id(value: str) -> str:
    value = (value or "").strip()
    if not OFFER_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="offer_id invalide")
    return value


def validate_candidate_email(value: str) -> str:
    value = (value or "").strip()
    if not EMAIL_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="candidate_email invalide")
    return value


def build_offer_url(offer_id: str) -> str:
    template = config.ts_offer_url_template()
    if "{offer_id}" not in template:
        raise HTTPException(status_code=500, detail="Erreur interne du serveur")
    url = template.format(base=config.ts_base_url(), offer_id=validate_offer_id(offer_id))
    return validate_talentsoft_url(url)


def offer_reference_matches(row_text: str, offer_id: str) -> bool:
    """Une ligne de candidature porte-t-elle la référence de l'offre visée ?

    Le Back Office affiche « Réponse à offre <intitulé> ( réf. <année>-<offerId> ) ».
    L'année n'est pas dérivable de `offer_id` : on reconnaît le suffixe `-<offer_id>`,
    délimité pour éviter qu'un identifiant court ne matche un numéro plus long
    (`-152` ne doit pas matcher `2026-25152`).
    """
    offer_id = (offer_id or "").strip()
    if not offer_id:
        return False
    normalized = re.sub(r"\s+", " ", row_text or "")
    return (
        re.search(rf"r.f\.\s*[A-Za-z0-9]*-?{re.escape(offer_id)}(?![0-9A-Za-z])", normalized, re.IGNORECASE) is not None
    )


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


def actions_may_have_mutated(actions: dict) -> bool:
    """True si une écriture a pu aboutir sans avoir pu être vérifiée.

    Le bot vérifie chaque écriture après coup. Quand la vérification échoue, il ne peut pas
    conclure : l'événement a peut-être été créé. L'appelant doit alors vérifier avant de
    rejouer, au lieu de créer un doublon dans le dossier du candidat.
    """
    if not actions:
        return False
    for result in actions.values():
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict) and item.get("mutation_may_have_happened"):
                return True
    return False
