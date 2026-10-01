from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.timezone import PERU_TIMEZONE, combine_peru_datetime, now_peru
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus


CONFIRMED_LIKE = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    # Legacy (unreachable): kept so old rows behave like a confirmed booking.
    BookingStatus.balance_pending,
    BookingStatus.balance_review,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
    BookingStatus.completed,
}


def booking_event_datetime(booking: Booking) -> datetime:
    return combine_peru_datetime(booking.event_date, booking.start_time)


def is_event_started(booking: Booking, *, now: datetime | None = None) -> bool:
    event_dt = booking_event_datetime(booking)
    if now is None:
        current = now_peru()
    elif now.tzinfo is None:
        current = now.replace(tzinfo=PERU_TIMEZONE)
    else:
        current = now.astimezone(PERU_TIMEZONE)
    return current >= event_dt


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