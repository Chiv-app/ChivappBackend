from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.limiter import limiter
from app.api import deps
from app.api.booking_helpers import (
    assert_booking_collaborator,
    get_booking_or_404,
)
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.user import User
from app.schemas.booking import (
    BookingGuestReviewCreate,
    BookingReviewOut,
    BookingShareOut,
    BookingSharePublicOut,
)
from app.services.booking_notifications import notify_booking_review
from app.services.booking_share import (
    disable_booking_share,
    format_guest_display_name,
    serialize_review,
    share_eligibility,
)
from app.services.uploads import save_upload

router = APIRouter(tags=["Booking share"])


def _get_shared_booking_or_410(db: Session, token: str) -> Booking:
    booking = (
        db.query(Booking)
        .filter(Booking.share_token == token, Booking.share_enabled.is_(True))
        .first()
    )
    if not booking:
        raise HTTPException(
            410,
            "Este enlace ya no está disponible. La reserva finalizó o el compartir fue cancelado.",
        )
    if booking.status in {BookingStatus.completed, BookingStatus.cancelled}:
        disable_booking_share(booking)
        db.commit()
        raise HTTPException(
            410,
            "Este enlace ya no está disponible. La reserva finalizó o el compartir fue cancelado.",
        )
    ok, reason = share_eligibility(db, booking)
    if not ok:
        disable_booking_share(booking)
        db.commit()
        raise HTTPException(410, reason or "Este enlace ya no está disponible.")
    return booking


def _musician_stage_name(db: Session, booking: Booking) -> str | None:
    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    return musician.stage_name if musician else None


@router.get("/bookings/{booking_id}/share", response_model=BookingShareOut)
def get_booking_share(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    can_enable, reason = share_eligibility(db, booking)
    enabled = bool(booking.share_enabled and booking.share_token and can_enable)
    if booking.share_enabled and not can_enable:
        disable_booking_share(booking)
        db.commit()
        enabled = False
    token = booking.share_token if enabled else None
    return BookingShareOut(
        booking_id=booking.id,
        enabled=enabled,
        can_enable=can_enable and not enabled,
        token=token,
        path=f"/share/{token}" if token else None,
        reason=None if (enabled or can_enable) else reason,
        share_enabled_at=booking.share_enabled_at if enabled else None,
    )


@router.post("/bookings/{booking_id}/share/enable", response_model=BookingShareOut)
def enable_booking_share(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    can_enable, reason = share_eligibility(db, booking)
    if not can_enable:
        raise HTTPException(400, reason or "No se puede habilitar el compartir.")

    if not booking.share_token or not booking.share_enabled:
        booking.share_token = secrets.token_urlsafe(24)
        booking.share_enabled = True
        booking.share_enabled_at = datetime.utcnow()
        db.commit()
        db.refresh(booking)

    return BookingShareOut(
        booking_id=booking.id,
        enabled=True,
        can_enable=False,
        token=booking.share_token,
        path=f"/share/{booking.share_token}",
        reason=None,
        share_enabled_at=booking.share_enabled_at,
    )


@router.post("/bookings/{booking_id}/share/disable", response_model=BookingShareOut)
def disable_booking_share_endpoint(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    disable_booking_share(booking)
    db.commit()
    can_enable, reason = share_eligibility(db, booking)
    return BookingShareOut(
        booking_id=booking.id,
        enabled=False,
        can_enable=can_enable,
        token=None,
        path=None,
        reason=None if can_enable else reason,
        share_enabled_at=None,
    )


@router.get("/public/share/{token}", response_model=BookingSharePublicOut)
def get_public_share(token: str, db: Session = Depends(deps.get_db)):
    booking = _get_shared_booking_or_410(db, token)
    reactions_open = booking.status in {
        BookingStatus.payment_retained,
        BookingStatus.change_pending,
        BookingStatus.in_progress,
        BookingStatus.payment_released,
    }
    return BookingSharePublicOut(
        token=token,
        event_type=booking.event_type,
        event_date=booking.event_date,
        start_time=booking.start_time,
        end_time=booking.end_time,
        location_city=booking.location_city,
        musician_name=_musician_stage_name(db, booking),
        status=booking.status,
        reactions_open=reactions_open,
        message="Comparte tu reacción del show. Solo necesitas tu nombre.",
    )


@router.get("/public/share/{token}/reviews", response_model=list[BookingReviewOut])
def list_public_share_reviews(token: str, db: Session = Depends(deps.get_db)):
    booking = _get_shared_booking_or_410(db, token)
    reviews = (
        db.query(BookingReview)
        .filter(BookingReview.booking_id == booking.id)
        .order_by(BookingReview.created_at.asc())
        .all()
    )
    return [serialize_review(review) for review in reviews]


@router.post("/public/share/{token}/reviews", response_model=BookingReviewOut)
@limiter.limit(settings.RATE_LIMIT_PUBLIC)
def create_public_share_review(
    request: Request,
    token: str,
    payload: BookingGuestReviewCreate,
    db: Session = Depends(deps.get_db),
):
    booking = _get_shared_booking_or_410(db, token)
    raw_name = " ".join(payload.guest_name.strip().split())
    if len(raw_name.replace(" - Invitado", "").strip()) < 2:
        raise HTTPException(400, "Ingresa un nombre válido.")
    guest_name = format_guest_display_name(raw_name)

    review = BookingReview(
        booking_id=booking.id,
        author_user_id=None,
        guest_name=guest_name,
        rating=payload.rating,
        emoji=payload.emoji,
        comment=payload.comment,
        photo_urls=payload.photo_urls or [],
        video_urls=payload.video_urls or [],
    )
    db.add(review)

    preview = payload.comment or f"{guest_name} dejó una reacción del evento"
    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    contractor = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if musician:
        musician_user = db.query(User).filter(User.id == musician.user_id).first()
        if musician_user:
            notify_booking_review(db, musician_user, str(booking.id), preview)
    if contractor:
        contractor_user = db.query(User).filter(User.id == contractor.user_id).first()
        if contractor_user:
            notify_booking_review(db, contractor_user, str(booking.id), preview)

    db.commit()
    db.refresh(review)
    return serialize_review(review)


@router.post("/public/share/{token}/upload")
@limiter.limit(settings.RATE_LIMIT_PUBLIC)
async def upload_public_share_file(
    request: Request,
    token: str,
    file: UploadFile = File(...),
    db: Session = Depends(deps.get_db),
):
    _get_shared_booking_or_410(db, token)
    try:
        url = await save_upload(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"url": url}
