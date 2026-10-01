from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingReview, BookingStatus
from app.schemas.booking import BookingReviewOut

SHAREABLE_STATUSES = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
}


def disable_booking_share(booking: Booking) -> None:
    booking.share_enabled = False
    booking.share_token = None
    booking.share_enabled_at = None


def share_eligibility(db: Session, booking: Booking) -> tuple[bool, str | None]:
    if booking.status in {BookingStatus.completed, BookingStatus.cancelled}:
        return False, "La reserva ya finalizó o fue cancelada. El enlace quedó desactivado."
    if booking.status not in SHAREABLE_STATUSES:
        return False, "El compartir se habilita cuando la reserva está pagada y confirmada."
    return True, None


GUEST_NAME_SUFFIX = " - Invitado"


def format_guest_display_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return cleaned
    lower = cleaned.casefold()
    suffix = GUEST_NAME_SUFFIX.casefold()
    if lower.endswith(suffix):
        cleaned = cleaned[: -len(GUEST_NAME_SUFFIX)].rstrip(" -")
    base = cleaned or "Invitado"
    return f"{base}{GUEST_NAME_SUFFIX}"


def review_author_label(review: BookingReview) -> str:
    if getattr(review, "guest_name", None):
        return format_guest_display_name(review.guest_name)
    author = getattr(review, "author", None)
    if author and getattr(author, "fullname", None):
        return author.fullname
    if author and getattr(author, "email", None):
        return author.email
    label = getattr(review, "author_label", None)
    if label:
        return label
    return "Participante"


def serialize_review(review: BookingReview | BookingReviewOut) -> BookingReviewOut:
    if isinstance(review, BookingReviewOut):
        return review
    return BookingReviewOut(
        id=review.id,
        booking_id=review.booking_id,
        author_user_id=review.author_user_id,
        guest_name=review.guest_name,
        author_label=review_author_label(review),
        rating=review.rating,
        emoji=review.emoji,
        comment=review.comment,
        photo_urls=review.photo_urls,
        video_urls=review.video_urls,
        is_final=bool(review.is_final),
        created_at=review.created_at,
        updated_at=review.updated_at,
    )
