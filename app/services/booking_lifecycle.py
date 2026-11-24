from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.timezone import PERU_TIMEZONE, combine_peru_datetime, now_peru
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus


CONFIRMED_LIKE = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    # Legacy (unreachable): kept so old rows behave like a confirmed booking.
    BookingStatus.in_progress,
    BookingStatus.payment_released,
    BookingStatus.completed,
}


DEFAULT_EVENT_DURATION_MINUTES = 120
REVIEW_GRACE_HOURS = 24


def booking_event_datetime(booking: Booking) -> datetime:
    return combine_peru_datetime(booking.event_date, booking.start_time)


def booking_event_end_datetime(booking: Booking) -> datetime:
    start_dt = booking_event_datetime(booking)
    if booking.end_time is not None:
        end_dt = combine_peru_datetime(booking.event_date, booking.end_time)
        if booking.end_time <= booking.start_time:
            end_dt += timedelta(days=1)
        return end_dt
    return start_dt + timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)


def is_event_started(
    booking: Booking,
    *,
    now: datetime | None = None,
    tolerance_minutes: int = 0,
) -> bool:
    event_dt = booking_event_datetime(booking) - timedelta(minutes=tolerance_minutes)
    if now is None:
        current = now_peru()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=PERU_TIMEZONE)
    else:
        current = now.astimezone(PERU_TIMEZONE)
    return current >= event_dt


def is_event_finished(
    booking: Booking,
    *,
    now: datetime | None = None,
) -> bool:
    if not is_event_started(booking, now=now):
        return False
    end_dt = booking_event_end_datetime(booking)
    if now is None:
        current = now_peru()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=PERU_TIMEZONE)
    else:
        current = now.astimezone(PERU_TIMEZONE)
    return current >= end_dt


def is_review_grace_period_expired(
    booking: Booking,
    *,
    now: datetime | None = None,
    hours: int = REVIEW_GRACE_HOURS,
) -> bool:
    if not is_event_finished(booking, now=now):
        return False
    end_dt = booking_event_end_datetime(booking)
    if now is None:
        current = now_peru()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=PERU_TIMEZONE)
    else:
        current = now.astimezone(PERU_TIMEZONE)
    return current >= (end_dt + timedelta(hours=hours))


def assert_event_started(
    booking: Booking,
    *,
    tolerance_minutes: int = 0,
    detail: str = "Esta acción solo está disponible cuando inicia la fecha y hora del evento.",
) -> None:
    if not is_event_started(booking, tolerance_minutes=tolerance_minutes):
        raise HTTPException(status_code=400, detail=detail)


def assert_event_finished(
    booking: Booking,
    *,
    detail: str = "Esta acción solo está disponible cuando haya finalizado el evento.",
) -> None:
    if not is_event_finished(booking):
        raise HTTPException(status_code=400, detail=detail)


def is_pre_event(booking: Booking, *, now: datetime | None = None) -> bool:
    return not is_event_started(booking, now=now)


def retained_paid_total(db: Session, booking_id) -> Decimal:
    payments = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status.in_([PaymentStatus.retained, PaymentStatus.released]),
        )
        .all()
    )
    total = Decimal("0")
    for payment in payments:
        total += Decimal(str(payment.amount))
    return total


def clear_pending_changes(booking: Booking) -> None:
    booking.pending_location_address = None
    booking.pending_location_city = None
    booking.pending_location_reference = None
    booking.pending_event_description = None
    booking.pending_change_notes = None
    booking.pending_price_agreed = None
    booking.change_requested_by = None
    booking.change_requested_at = None


def apply_pending_changes(booking: Booking) -> None:
    if booking.pending_location_address:
        booking.location_address = booking.pending_location_address
    if booking.pending_location_city is not None:
        booking.location_city = booking.pending_location_city
    if booking.pending_location_reference is not None:
        booking.location_reference = booking.pending_location_reference
    if booking.pending_event_description is not None:
        booking.event_description = booking.pending_event_description
    price_changed = booking.pending_price_agreed is not None
    if booking.pending_price_agreed is not None:
        booking.price_agreed = booking.pending_price_agreed
    clear_pending_changes(booking)
    if price_changed:
        from app.services.platform_payment import sync_booking_platform_fee

        # Keep existing percent snapshot; only recompute absolute fee.
        sync_booking_platform_fee(None, booking, use_current_settings=False)


def apply_commitment_fields(
    booking: Booking,
    db: Session | None = None,
    *,
    location_address: str | None = None,
    location_city: str | None = None,
    location_reference: str | None = None,
    event_description: str | None = None,
    price_agreed: Decimal | None = None,
) -> None:
    """Aplica campos del compromiso de inmediato (sin flujo de validación)."""
    if location_address is not None:
        booking.location_address = location_address
    if location_city is not None:
        booking.location_city = location_city
    if location_reference is not None:
        booking.location_reference = location_reference
    if event_description is not None:
        booking.event_description = event_description
    price_changed = price_agreed is not None
    if price_agreed is not None:
        booking.price_agreed = price_agreed
    if price_changed:
        from app.services.platform_payment import sync_booking_platform_fee

        sync_booking_platform_fee(
            db,
            booking,
            use_current_settings=db is not None,
        )


QUOTE_EXPIRATION_HOURS = 48
QUOTE_STAGE_STATUSES = {
    BookingStatus.accepted,
    BookingStatus.contract_pending,
}


def is_quote_stage_expired(
    booking: Booking,
    *,
    now_utc: datetime | None = None,
    hours: int = QUOTE_EXPIRATION_HOURS,
) -> bool:
    if booking.status not in QUOTE_STAGE_STATUSES:
        return False
    ref_dt = booking.quoted_at or booking.updated_at or booking.created_at
    if ref_dt is None:
        return False
    from datetime import timezone as _tz

    if ref_dt.tzinfo is not None:
        ref_dt = ref_dt.astimezone(_tz.utc).replace(tzinfo=None)
    current_utc = now_utc or datetime.utcnow()
    if current_utc.tzinfo is not None:
        current_utc = current_utc.astimezone(_tz.utc).replace(tzinfo=None)
    return current_utc >= (ref_dt + timedelta(hours=hours))


def expire_stale_quote_bookings(
    db: Session,
    bookings: list[Booking],
    *,
    commit: bool = True,
) -> list[Booking]:
    """Cancela automáticamente las reservas que llevan más de 48h en etapa de Cotización y acuerdo."""
    from app.models.booking_link import BookingLink, BookingLinkStatus

    candidates = [b for b in bookings if is_quote_stage_expired(b)]
    if not candidates:
        return []

    candidate_ids = [b.id for b in candidates]
    paid_booking_ids = {
        row[0]
        for row in db.query(Payment.booking_id)
        .filter(
            Payment.booking_id.in_(candidate_ids),
            Payment.payment_type.is_distinct_from("refund"),
            Payment.status.in_([PaymentStatus.retained, PaymentStatus.released]),
        )
        .all()
    }

    expired: list[Booking] = []
    for booking in candidates:
        if booking.id in paid_booking_ids:
            continue
        booking.status = BookingStatus.cancelled
        booking.cancelled_by = "system"
        booking.rejection_reason = (
            "Cancelada automáticamente por superar las 48 horas en etapa de cotización y acuerdo sin confirmación."
        )
        booking.cancellation_refund_percent = 0
        booking.cancellation_refund_amount = Decimal("0.00")
        booking.cancellation_refund_status = "not_applicable"
        expired.append(booking)

    if expired:
        expired_ids = [b.id for b in expired]
        for bl in (
            db.query(BookingLink)
            .filter(BookingLink.booking_id.in_(expired_ids))
            .all()
        ):
            if bl.status != BookingLinkStatus.completed:
                bl.status = BookingLinkStatus.expired
        if commit:
            db.commit()
            for b in expired:
                db.refresh(b)

    return expired
