from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.services.email.defaults import APP_NAME
from app.services.email.service import send_templated_email

EMAIL_VERIFICATION_TTL_HOURS = 48
PASSWORD_RESET_TTL_HOURS = 1


def _frontend_url(path: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}{path}"


def issue_email_verification_token(user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.email_verification_token = token
    user.email_verification_expires_at = datetime.utcnow() + timedelta(
        hours=EMAIL_VERIFICATION_TTL_HOURS
    )
    return token


def issue_password_reset_token(user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.password_reset_token = token
    user.password_reset_expires_at = datetime.utcnow() + timedelta(
        hours=PASSWORD_RESET_TTL_HOURS
    )
    return token


def mark_email_verified(user: User) -> None:
    user.email_verified_at = datetime.utcnow()
    user.email_verification_token = None
    user.email_verification_expires_at = None


def is_email_verified(user: User) -> bool:
    return user.email_verified_at is not None


def send_welcome_email(db: Session, user: User) -> None:
    send_templated_email(
        db,
        slug="welcome",
        to=user.email,
        context={
            "user_name": user.fullname,
            "user_email": user.email,
            "app_name": APP_NAME,
            "login_url": _frontend_url("/"),
        },
        user_id=user.id,
    )


def send_email_verification(db: Session, user: User) -> None:
    token = issue_email_verification_token(user)
    send_templated_email(
        db,
        slug="email_verification",
        to=user.email,
        context={
            "user_name": user.fullname,
            "user_email": user.email,
            "app_name": APP_NAME,
            "action_url": _frontend_url(f"/verify-email?token={token}"),
            "expires_hours": str(EMAIL_VERIFICATION_TTL_HOURS),
        },
        user_id=user.id,
    )


def send_password_reset_email(db: Session, user: User) -> None:
    token = issue_password_reset_token(user)
    send_templated_email(
        db,
        slug="password_reset",
        to=user.email,
        context={
            "user_name": user.fullname,
            "user_email": user.email,
            "app_name": APP_NAME,
            "action_url": _frontend_url(f"/reset-password?token={token}"),
            "expires_hours": str(PASSWORD_RESET_TTL_HOURS),
        },
        user_id=user.id,
    )


def send_ensemble_invite_email(
    db: Session,
    *,
    member_email: str,
    member_name: str,
    leader_name: str,
    invite_url: str,
    specialties: list[str],
    user_id: UUID | None,
    expires_days: int,
) -> None:
    send_templated_email(
        db,
        slug="ensemble_invite",
        to=member_email,
        context={
            "member_name": member_name,
            "member_email": member_email,
            "leader_name": leader_name,
            "app_name": APP_NAME,
            "action_url": invite_url,
            "expires_days": str(expires_days),
            "specialties": ", ".join(specialties) if specialties else "—",
        },
        user_id=user_id,
    )


def send_booking_member_invite_email(
    db: Session,
    *,
    member_email: str,
    member_name: str,
    leader_name: str,
    event_type: str,
    event_date: str,
    event_time: str,
    event_location: str,
    respond_url: str,
    user_id: UUID | None,
    booking_id: str,
    extra_attachments: list[dict] | None = None,
) -> None:
    send_templated_email(
        db,
        slug="booking_member_invite",
        to=member_email,
        context={
            "member_name": member_name,
            "leader_name": leader_name,
            "app_name": APP_NAME,
            "event_type": event_type,
            "event_date": event_date,
            "event_time": event_time,
            "event_location": event_location,
            "action_url": respond_url,
        },
        user_id=user_id,
        meta={"booking_id": booking_id},
        extra_attachments=extra_attachments,
    )


def send_booking_member_accepted_to_leader_email(
    db: Session,
    *,
    leader_email: str,
    leader_name: str,
    member_name: str,
    event_type: str,
    event_date: str,
    event_time: str,
    user_id: UUID | None,
    booking_id: str,
) -> None:
    send_templated_email(
        db,
        slug="booking_member_accepted_leader",
        to=leader_email,
        context={
            "leader_name": leader_name,
            "member_name": member_name,
            "app_name": APP_NAME,
            "event_type": event_type,
            "event_date": event_date,
            "event_time": event_time,
        },
        user_id=user_id,
        meta={"booking_id": booking_id},
    )


def verify_email_token(db: Session, token: str) -> User:
    user = (
        db.query(User)
        .filter(User.email_verification_token == token)
        .first()
    )
    if not user:
        raise ValueError("invalid")
    if (
        user.email_verification_expires_at
        and user.email_verification_expires_at < datetime.utcnow()
    ):
        raise ValueError("expired")
    mark_email_verified(user)
    return user


def find_password_reset_user(db: Session, token: str) -> User:
    user = db.query(User).filter(User.password_reset_token == token).first()
    if not user:
        raise ValueError("invalid")
    if (
        user.password_reset_expires_at
        and user.password_reset_expires_at < datetime.utcnow()
    ):
        raise ValueError("expired")
    return user


def clear_password_reset(user: User) -> None:
    user.password_reset_token = None
    user.password_reset_expires_at = None
