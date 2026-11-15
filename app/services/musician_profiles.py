"""Varios perfiles artísticos por persona: cuál es «el activo», cuántos puede tener y qué datos comparten.

Un músico puede tener hasta `MAX_PROFILES_PER_USER` perfiles públicos (por ejemplo, solista y su grupo).
Los datos de la persona (documento, firma y cuenta de cobro) se piden una sola vez: se escriben en el perfil
que se edita y se copian a los demás perfiles de la misma persona.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.active_profile import active_profile_id
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User

MAX_PROFILES_PER_USER = 3

# Datos de la persona: iguales en todos sus perfiles. El nombre completo vive en la cuenta.
PERSON_FIELDS: tuple[str, ...] = (
    "id_document_url",
    "signature_image_url",
    "payout_method",
    "payout_bank_name",
    "payout_account_number",
    "payout_cci",
    "payout_phone",
    "payout_beneficiary_name",
    "payout_beneficiary_document",
    "payout_mp_email",
)


def list_user_profiles(db: Session, user: User) -> list[MusicianProfile]:
    """Perfiles de la persona, el principal primero."""
    return (
        db.query(MusicianProfile)
        .filter(MusicianProfile.user_id == user.id)
        .order_by(MusicianProfile.is_primary.desc(), MusicianProfile.created_at.asc(), MusicianProfile.id.asc())
        .all()
    )


def user_profile_ids(db: Session, user: User) -> list[UUID]:
    return [pid for (pid,) in db.query(MusicianProfile.id).filter(MusicianProfile.user_id == user.id).all()]


def count_user_profiles(db: Session, user: User) -> int:
    return db.query(func.count(MusicianProfile.id)).filter(MusicianProfile.user_id == user.id).scalar() or 0


def user_owns_profile(db: Session, user: User, profile_id: UUID | None) -> bool:
    if profile_id is None:
        return False
    return (
        db.query(MusicianProfile.id)
        .filter(MusicianProfile.id == profile_id, MusicianProfile.user_id == user.id)
        .first()
        is not None
    )


def resolve_active_profile(db: Session, user: User) -> MusicianProfile | None:
    """El perfil con el que trabaja la persona en esta petición.

    Se usa el que eligió la app (cabecera `X-Musician-Profile-Id`) solo si es suyo; si no, el principal.
    """
    wanted = active_profile_id.get()
    base = db.query(MusicianProfile).filter(MusicianProfile.user_id == user.id)
    if wanted is not None:
        chosen = base.filter(MusicianProfile.id == wanted).first()
        if chosen is not None:
            return chosen
    return base.order_by(MusicianProfile.is_primary.desc(), MusicianProfile.created_at.asc(), MusicianProfile.id.asc()).first()


def copy_person_data(source: MusicianProfile, target: MusicianProfile) -> None:
    for field in PERSON_FIELDS:
        setattr(target, field, getattr(source, field))


def sibling_profiles(db: Session, profile: MusicianProfile) -> list[MusicianProfile]:
    """Los otros perfiles de la misma persona."""
    return (
        db.query(MusicianProfile)
        .filter(MusicianProfile.user_id == profile.user_id, MusicianProfile.id != profile.id)
        .all()
    )


def user_has_other_published_profile(user: User, excluding: MusicianProfile | None = None) -> bool:
    """Si la persona sigue teniendo algún perfil publicado además de `excluding`."""
    return any(
        p.status == ProfileStatus.published and (excluding is None or p.id != excluding.id)
        for p in getattr(user, "musician_profiles", [])
    )


def members_of_profile_clause(profile: MusicianProfile):
    """Condición SQL de los integrantes de un perfil (grupo).

    Los integrantes anteriores a este dato no tienen perfil y cuentan para el principal de su líder.
    """
    from app.models.ensemble_member import EnsembleMember

    clauses = [EnsembleMember.profile_id == profile.id]
    if profile.is_primary:
        clauses.append(
            and_(EnsembleMember.profile_id.is_(None), EnsembleMember.leader_user_id == profile.user_id)
        )
    return or_(*clauses)


def member_belongs_to_profile(member, profile: MusicianProfile) -> bool:
    """Si el integrante pertenece a ese perfil (los de antes de este dato, al principal de su líder)."""
    if member.profile_id is not None:
        return member.profile_id == profile.id
    return bool(profile.is_primary and member.leader_user_id == profile.user_id)


def public_team(db: Session, profile: MusicianProfile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Integrantes del grupo y grupos en los que toca la persona, para el perfil público.

    Solo cuentan integrantes activos (los que aceptaron) y perfiles publicados y verificados. Nunca se
    muestran correo, teléfono ni datos de cobro.
    """
    from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus

    members: list[dict[str, Any]] = []
    if (profile.profile_type or "solo") == "group":
        rows = (
            db.query(EnsembleMember)
            .filter(members_of_profile_clause(profile), EnsembleMember.status == EnsembleMemberStatus.active)
            .order_by(EnsembleMember.fullname.asc())
            .all()
        )
        user_ids = [r.member_user_id for r in rows if r.member_user_id]
        solo_slug: dict[UUID, str] = {}
        if user_ids:
            solos = (
                db.query(MusicianProfile)
                .join(User, User.id == MusicianProfile.user_id)
                .filter(
                    MusicianProfile.user_id.in_(user_ids),
                    MusicianProfile.profile_type == "solo",
                    MusicianProfile.is_ensemble_only.is_(False),
                    MusicianProfile.status == ProfileStatus.published,
                    MusicianProfile.slug.isnot(None),
                    User.is_verified.is_(True),
                )
                .order_by(MusicianProfile.is_primary.desc(), MusicianProfile.created_at.asc())
                .all()
            )
            for solo in solos:
                solo_slug.setdefault(solo.user_id, solo.slug)
        members = [
            {"name": r.fullname, "specialties": list(r.specialties or []), "profile_slug": solo_slug.get(r.member_user_id)}
            for r in rows
        ]

    plays_with: list[dict[str, Any]] = []
    if (profile.profile_type or "solo") == "solo":
        from sqlalchemy import and_, or_

        groups = (
            db.query(MusicianProfile)
            .join(User, User.id == MusicianProfile.user_id)
            .join(
                EnsembleMember,
                or_(
                    EnsembleMember.profile_id == MusicianProfile.id,
                    and_(
                        EnsembleMember.profile_id.is_(None),
                        EnsembleMember.leader_user_id == MusicianProfile.user_id,
                        MusicianProfile.is_primary.is_(True),
                    ),
                ),
            )
            .filter(
                EnsembleMember.member_user_id == profile.user_id,
                EnsembleMember.status == EnsembleMemberStatus.active,
                MusicianProfile.id != profile.id,
                MusicianProfile.profile_type == "group",
                MusicianProfile.status == ProfileStatus.published,
                MusicianProfile.slug.isnot(None),
                User.is_verified.is_(True),
            )
            .order_by(MusicianProfile.stage_name.asc())
            .all()
        )
        plays_with = [{"name": g.stage_name or "", "slug": g.slug} for g in groups]
    return members, plays_with


def profile_summary(profile: MusicianProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "stage_name": profile.stage_name,
        "slug": profile.slug,
        "profile_type": profile.profile_type or "solo",
        "group_size": profile.group_size,
        "status": profile.status,
        "is_primary": bool(profile.is_primary),
        "profile_image_url": profile.profile_image_url,
    }
