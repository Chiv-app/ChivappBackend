"""Unicidad de identificadores de cuenta y perfil."""

from __future__ import annotations

import re
import unicodedata
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.user import User


def normalize_email(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned or None


def normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    # Conserva dígitos y + inicial; quita espacios y guiones; unifica +51XXXXXXXXX -> XXXXXXXXX.
    raw = str(value).strip()
    if not raw:
        return None
    chars: list[str] = []
    for i, ch in enumerate(raw):
        if ch.isdigit():
            chars.append(ch)
        elif ch == "+" and i == 0:
            chars.append(ch)
    cleaned = "".join(chars)
    if cleaned.startswith("+51") and len(cleaned) == 12 and cleaned[3:].isdigit():
        cleaned = cleaned[3:]
    elif cleaned.startswith("51") and len(cleaned) == 11 and cleaned[2] == "9" and cleaned.isdigit():
        cleaned = cleaned[2:]
    return cleaned or None


def normalize_document_number(value: str | None) -> str | None:
    cleaned = (value or "").strip().upper().replace(" ", "")
    return cleaned or None


def normalize_stage_name(value: str | None) -> str | None:
    cleaned = " ".join((value or "").strip().split())
    return cleaned or None


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    # Convierte espacios a guiones y remueve tildes / caracteres no válidos
    cleaned = re.sub(r"\s+", "-", raw)
    normalized = unicodedata.normalize("NFKD", cleaned)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug_val = re.sub(r"[^a-z0-9_-]", "", ascii_only).strip("-_")
    return slug_val or None


def assert_username_unique(
    db: Session,
    username: str | None,
    *,
    exclude_user_id: UUID | None = None,
) -> str:
    norm = normalize_username(username)
    if not norm or len(norm) < 3:
        raise HTTPException(
            400,
            "El nombre de usuario debe tener al menos 3 caracteres y contener solo letras, números o guiones.",
        )

    # 1. Verificar si ya existe en User.username
    user_query = db.query(User.id).filter(
        func.lower(func.btrim(User.username)) == norm
    )
    if exclude_user_id is not None:
        user_query = user_query.filter(User.id != exclude_user_id)
    if user_query.first():
        raise HTTPException(
            400,
            "Ya existe un músico registrado con ese nombre de usuario",
        )

    # 2. Verificar si ya existe en MusicianProfile.slug
    profile_query = db.query(MusicianProfile.id).filter(
        func.lower(func.btrim(MusicianProfile.slug)) == norm
    )
    if exclude_user_id is not None:
        profile_query = profile_query.join(User).filter(User.id != exclude_user_id)
    if profile_query.first():
        raise HTTPException(
            400,
            "Ya existe un músico registrado con ese nombre de usuario",
        )

    # 3. Verificar si ya existe en MusicianProfile.stage_name
    stage_query = db.query(MusicianProfile.id).filter(
        func.lower(func.btrim(MusicianProfile.stage_name)) == norm
    )
    if exclude_user_id is not None:
        stage_query = stage_query.join(User).filter(User.id != exclude_user_id)
    if stage_query.first():
        raise HTTPException(
            400,
            "Ya existe un músico registrado con ese nombre",
        )

    return norm


def _username_taken(
    db: Session,
    username: str,
    *,
    exclude_user_id: UUID | None = None,
) -> bool:
    norm = normalize_username(username)
    if not norm:
        return True
    user_query = db.query(User.id).filter(
        func.lower(func.btrim(User.username)) == norm
    )
    if exclude_user_id is not None:
        user_query = user_query.filter(User.id != exclude_user_id)
    if user_query.first():
        return True

    profile_query = db.query(MusicianProfile.id).filter(
        func.lower(func.btrim(MusicianProfile.slug)) == norm
    )
    if exclude_user_id is not None:
        profile_query = profile_query.join(User).filter(User.id != exclude_user_id)
    if profile_query.first():
        return True

    stage_query = db.query(MusicianProfile.id).filter(
        func.lower(func.btrim(MusicianProfile.stage_name)) == norm
    )
    if exclude_user_id is not None:
        stage_query = stage_query.join(User).filter(User.id != exclude_user_id)
    if stage_query.first():
        return True

    return False


def next_available_username(
    db: Session,
    preferred: str | None,
    *,
    exclude_user_id: UUID | None = None,
) -> str:
    """Genera un username único para registros automáticos (p. ej. OAuth)."""
    base = normalize_username(preferred) or "musico"
    if len(base) < 3:
        base = f"{base}-1" if len(base) >= 1 else "musico"
    if not _username_taken(db, base, exclude_user_id=exclude_user_id):
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if not _username_taken(db, candidate, exclude_user_id=exclude_user_id):
            return candidate
    return f"{base}-{uuid4().hex[:6]}"


def assert_musician_slug_unique(
    db: Session,
    slug: str | None,
    *,
    exclude_profile_id: UUID | None = None,
) -> str:
    norm = normalize_username(slug)
    if not norm:
        raise HTTPException(400, "El slug del perfil es inválido o está vacío")
    if _musician_slug_taken(db, norm, exclude_profile_id=exclude_profile_id):
        raise HTTPException(
            400,
            "Ya existe un músico registrado con ese nombre de usuario o enlace",
        )
    return norm


def _stage_name_taken(
    db: Session,
    stage_name: str,
    *,
    exclude_profile_id: UUID | None = None,
) -> bool:
    query = db.query(MusicianProfile.id).filter(
        func.lower(func.btrim(MusicianProfile.stage_name)) == stage_name.lower()
    )
    if exclude_profile_id is not None:
        query = query.filter(MusicianProfile.id != exclude_profile_id)
    return query.first() is not None


def next_available_stage_name(db: Session, preferred: str | None) -> str:
    """Para borradores automáticos: evita choque sin fallar el registro."""
    base = normalize_stage_name(preferred) or "Artista"
    if not _stage_name_taken(db, base):
        return base
    for suffix in range(2, 1000):
        candidate = f"{base} ({suffix})"
        if not _stage_name_taken(db, candidate):
            return candidate
    return f"{base} ({uuid4().hex[:6]})"


def assert_email_unique(
    db: Session,
    email: str | None,
    *,
    exclude_user_id: UUID | None = None,
) -> str | None:
    norm = normalize_email(email)
    if not norm:
        return None
    query = db.query(User).filter(func.lower(User.email) == norm)
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    if query.first():
        raise HTTPException(
            400,
            "No se puede utilizar este correo porque ya está en uso.",
        )
    return norm


def assert_phone_unique(
    db: Session,
    phone: str | None,
    *,
    exclude_user_id: UUID | None = None,
) -> str | None:
    norm = normalize_phone(phone)
    if not norm:
        return None
    candidates = db.query(User).filter(
        User.phone.isnot(None),
        ~User.email.like("%@guest.local"),
    )
    if exclude_user_id is not None:
        candidates = candidates.filter(User.id != exclude_user_id)
    for row in candidates.all():
        if normalize_phone(row.phone) == norm:
            raise HTTPException(400, "Ya existe una cuenta con ese número de teléfono")
    return norm


def assert_stage_name_unique(
    db: Session,
    stage_name: str | None,
    *,
    exclude_profile_id: UUID | None = None,
) -> str | None:
    norm = normalize_stage_name(stage_name)
    if not norm:
        return None
    if _stage_name_taken(db, norm, exclude_profile_id=exclude_profile_id):
        raise HTTPException(400, "Ya existe un perfil con ese nombre de artista")
    return norm


def slugify(value: str) -> str:
    """Convierte un nombre a un slug de URL: minúsculas, sin acentos/símbolos."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "musico"


def _musician_slug_taken(
    db: Session,
    slug: str,
    *,
    exclude_profile_id: UUID | None = None,
) -> bool:
    query = db.query(MusicianProfile.id).filter(MusicianProfile.slug == slug)
    if exclude_profile_id is not None:
        query = query.filter(MusicianProfile.id != exclude_profile_id)
    return query.first() is not None


def next_available_musician_slug(
    db: Session,
    stage_name: str | None,
    *,
    exclude_profile_id: UUID | None = None,
) -> str:
    base = slugify(stage_name or "musico")
    if not _musician_slug_taken(db, base, exclude_profile_id=exclude_profile_id):
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if not _musician_slug_taken(db, candidate, exclude_profile_id=exclude_profile_id):
            return candidate
    return f"{base}-{uuid4().hex[:6]}"


def assert_document_number_unique(
    db: Session,
    document_number: str | None,
    *,
    exclude_profile_id: UUID | None = None,
) -> str | None:
    norm = normalize_document_number(document_number)
    if not norm:
        return None
    query = db.query(ContractorProfile).filter(
        func.upper(func.replace(func.btrim(ContractorProfile.document_number), " ", ""))
        == norm
    )
    if exclude_profile_id is not None:
        query = query.filter(ContractorProfile.id != exclude_profile_id)
    matches = query.all()
    for match in matches:
        owner = db.get(User, match.user_id)
        if owner is not None and not owner.is_verified and owner.email_verified_at is None:
            match.document_number = None
            db.flush()
            continue
        raise HTTPException(
            400, "Ya existe una cuenta con ese número de documento (DNI)"
        )
    return norm
