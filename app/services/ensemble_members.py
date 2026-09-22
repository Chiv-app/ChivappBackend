from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    BookingMemberPayout,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.booking_notifications import notify_user
from app.services.uniqueness import (
    assert_phone_unique,
    next_available_musician_slug,
    next_available_stage_name,
)

PASSWORD_SETUP_TTL_DAYS = 14


def new_token() -> str:
    return secrets.token_urlsafe(32)


def password_setup_url(token: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/set-password?token={token}"


def booking_respond_url(token: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/invite/member/{token}"


def issue_password_setup_token(member: EnsembleMember) -> str:
    token = new_token()
    member.password_setup_token = token
    member.password_setup_expires_at = datetime.utcnow() + timedelta(
        days=PASSWORD_SETUP_TTL_DAYS
    )
    return token


def get_leader_musician_or_403(db: Session, user: User) -> MusicianProfile:
    if user.role != UserRole.musician:
        raise HTTPException(403, "Solo músicos pueden gestionar integrantes")
    profile = (
        db.query(MusicianProfile).filter(MusicianProfile.user_id == user.id).first()
    )
    if not profile:
        raise HTTPException(400, "Completa tu perfil de músico primero")
    if profile.status != ProfileStatus.published or not user.is_verified:
        raise HTTPException(
            403,
            "Tu perfil de músico debe estar verificado para gestionar integrantes",
        )
    return profile


def serialize_member(member: EnsembleMember, *, include_invite_url: bool = False) -> dict:
    linked = member.member_user
    has_password = bool(linked and linked.password_hash)
    invite_url = None
    invite_expires_at = None
    invite_expired = False
    # Mientras no cree contraseña, la invitación sigue pendiente y el enlace
    # debe poder copiarse (aunque esté vencido: la UI pedirá regenerarlo).
    if member.password_setup_token and not has_password:
        invite_expires_at = member.password_setup_expires_at
        invite_expired = bool(
            invite_expires_at and invite_expires_at < datetime.utcnow()
        )
        if include_invite_url:
            invite_url = password_setup_url(member.password_setup_token)
    return {
        "id": member.id,
        "email": member.email,
        "fullname": member.fullname,
        "phone": member.phone,
        "specialties": list(member.specialties or []),
        "notes": member.notes,
        "status": member.status.value if hasattr(member.status, "value") else member.status,
        "has_password": has_password,
        "invite_url": invite_url,
        "invite_expires_at": invite_expires_at,
        "invite_expired": invite_expired,
        "member_user_id": member.member_user_id,
        "invited_at": member.invited_at,
        "joined_at": member.joined_at,
        "created_at": member.created_at,
        "updated_at": member.updated_at,
    }


def create_or_link_member_user(
    db: Session,
    *,
    leader: User,
    email: str,
    fullname: str,
    phone: str | None,
    specialties: list[str],
) -> User:
    from app.services.uniqueness import normalize_email

    email_norm = normalize_email(email)
    if not email_norm:
        raise HTTPException(400, "El correo electrónico es obligatorio")

    existing = (
        db.query(User)
        .filter(func.lower(User.email) == email_norm)
        .first()
    )
    if existing:
        if existing.id == leader.id:
            raise HTTPException(400, "No puedes agregarte a ti mismo como integrante")
        if existing.role != UserRole.musician:
            raise HTTPException(
                400,
                "No se puede utilizar este correo porque ya está en uso.",
            )
        # Keep existing password; if none, leader can resend setup link.
        if phone and not existing.phone:
            existing.phone = assert_phone_unique(
                db, phone, exclude_user_id=existing.id
            )
        profile = (
            db.query(MusicianProfile)
            .filter(MusicianProfile.user_id == existing.id)
            .first()
        )
        if profile and specialties and not profile.instruments:
            profile.instruments = specialties
        return existing

    phone_norm = assert_phone_unique(db, phone)
    user = User(
        email=email_norm,
        fullname=fullname,
        password_hash=None,
        role=UserRole.musician,
        phone=phone_norm,
        # Trusted via leader invite; profile stays draft (not marketplace-listed).
        is_verified=True,
        is_active=True,
    )
    db.add(user)
    db.flush()
    stage_name = next_available_stage_name(db, fullname)
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=stage_name,
        slug=next_available_musician_slug(db, stage_name),
        genres=[],
        instruments=specialties,
        songs=[],
        repertoire=[],
        status=ProfileStatus.draft,
        is_ensemble_only=True,
    )
    db.add(profile)
    db.flush()
    return user


def find_password_setup_member(db: Session, token: str) -> EnsembleMember:
    member = (
        db.query(EnsembleMember)
        .filter(EnsembleMember.password_setup_token == token)
        .first()
    )
    if not member:
        raise HTTPException(404, "Enlace de invitación no válido")
    if (
        member.password_setup_expires_at
        and member.password_setup_expires_at < datetime.utcnow()
    ):
        raise HTTPException(410, "El enlace expiró. Pide al líder que reenvíe la invitación")
    if not member.member_user_id:
        raise HTTPException(400, "La invitación no está vinculada a una cuenta")
    return member


def activate_member_after_password(db: Session, member: EnsembleMember) -> None:
    member.status = EnsembleMemberStatus.active
    if not member.joined_at:
        member.joined_at = datetime.utcnow()
    member.password_setup_token = None
    member.password_setup_expires_at = None


def notify_leader_member_joined(db: Session, member: EnsembleMember) -> None:
    leader = db.get(User, member.leader_user_id)
    if not leader:
        return
    notify_user(
        db,
        user=leader,
        type="ensemble_member_joined",
        title="Integrante activó su cuenta",
        message=f"{member.fullname} ya creó su contraseña y forma parte de tu agrupación.",
        meta={"ensemble_member_id": str(member.id)},
    )


def serialize_booking_invite(invite: BookingMemberInvite) -> dict:
    member = invite.ensemble_member
    return {
        "id": invite.id,
        "booking_id": invite.booking_id,
        "ensemble_member_id": invite.ensemble_member_id,
        "member_fullname": member.fullname if member else "",
        "member_email": member.email if member else "",
        "specialties": list(member.specialties or []) if member else [],
        "status": invite.status.value if hasattr(invite.status, "value") else invite.status,
        "respond_url": booking_respond_url(invite.response_token)
        if invite.status == BookingMemberInviteStatus.pending
        else None,
        "invited_at": invite.invited_at,
        "responded_at": invite.responded_at,
    }


def serialize_payout(payout: BookingMemberPayout) -> dict:
    member = payout.ensemble_member
    return {
        "id": payout.id,
        "booking_id": payout.booking_id,
        "ensemble_member_id": payout.ensemble_member_id,
        "member_fullname": member.fullname if member else "",
        "member_email": member.email if member else "",
        "amount": payout.amount,
        "currency": payout.currency,
        "status": payout.status.value if hasattr(payout.status, "value") else payout.status,
        "note": payout.note,
        "set_by_leader_at": payout.set_by_leader_at,
        "paid_at": payout.paid_at,
    }


def get_member_for_leader(
    db: Session, *, leader_id: UUID, member_id: UUID
) -> EnsembleMember:
    member = (
        db.query(EnsembleMember)
        .filter(
            EnsembleMember.id == member_id,
            EnsembleMember.leader_user_id == leader_id,
        )
        .first()
    )
    if not member:
        raise HTTPException(404, "Integrante no encontrado")
    return member
