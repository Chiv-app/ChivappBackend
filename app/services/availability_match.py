"""Match booking date/time against musician weekly availability slots.

day_of_week convention (aligned with the musician wizard / JS Date.getDay):
0 = Domingo, 1 = Lunes, ..., 6 = Sábado.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.musician_availability import MusicianAvailability

ACTIVE_OVERLAP_STATUSES = (
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
)

DEFAULT_BOOKING_DURATION_MINUTES = 120

DAY_LABELS = (
    "Domingo",
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
)


def to_stored_day_of_week(event_date: date) -> int:
    """Python Monday=0 → stored Sunday=0."""
    return (event_date.weekday() + 1) % 7


def list_musician_slots(db: Session, musician_id) -> list[MusicianAvailability]:
    return (
        db.query(MusicianAvailability)
        .filter(MusicianAvailability.musician_id == musician_id)
        .order_by(
            MusicianAvailability.day_of_week,
            MusicianAvailability.start_time,
        )
        .all()
    )


def format_time(value: time) -> str:
    return value.strftime("%H:%M")


def slot_covers(slot: MusicianAvailability, start_time: time) -> bool:
    return slot.start_time <= start_time < slot.end_time


def matching_slots(
    slots: list[MusicianAvailability],
    event_date: date,
    start_time: time | None = None,
) -> list[MusicianAvailability]:
    dow = to_stored_day_of_week(event_date)
    day_slots = [slot for slot in slots if slot.day_of_week == dow]
    if start_time is None:
        return day_slots
    return [slot for slot in day_slots if slot_covers(slot, start_time)]


def _interval_minutes(start_time: time, end_time: time | None = None) -> tuple[int, int]:
    start_min = start_time.hour * 60 + start_time.minute
    if end_time is not None:
        end_min = end_time.hour * 60 + end_time.minute
        if end_min <= start_min:
            end_min += 24 * 60
    else:
        end_min = start_min + DEFAULT_BOOKING_DURATION_MINUTES
    return start_min, end_min


def assert_no_booking_overlap(
    db: Session,
    musician_id,
    event_date: date,
    start_time: time,
    end_time: time | None = None,
    *,
    exclude_booking_id=None,
) -> None:
    query = db.query(Booking).filter(
        Booking.musician_id == musician_id,
        Booking.event_date.in_(
            [
                event_date - timedelta(days=1),
                event_date,
                event_date + timedelta(days=1),
            ]
        ),
        Booking.status.in_(ACTIVE_OVERLAP_STATUSES),
    )
    if exclude_booking_id is not None:
        query = query.filter(Booking.id != exclude_booking_id)

    existing_bookings = query.all()
    if not isinstance(existing_bookings, list):
        return

    req_start, req_end = _interval_minutes(start_time, end_time)
    for other in existing_bookings:
        if not isinstance(other, Booking) or not getattr(other, "start_time", None):
            continue
        other_start, other_end = _interval_minutes(other.start_time, getattr(other, "end_time", None))
        other_date = getattr(other, "event_date", None)
        if isinstance(other_date, date):
            day_offset_min = (other_date - event_date).days * 24 * 60
            other_start += day_offset_min
            other_end += day_offset_min
        if req_start < other_end and other_start < req_end:
            raise HTTPException(
                400,
                "El músico ya tiene una reserva activa en ese horario.",
            )


def assert_musician_available(
    db: Session,
    musician_id,
    event_date: date,
    start_time: time,
    end_time: time | None = None,
    *,
    exclude_booking_id=None,
) -> None:
    assert_no_booking_overlap(
        db,
        musician_id,
        event_date,
        start_time,
        end_time=end_time,
        exclude_booking_id=exclude_booking_id,
    )

    slots = list_musician_slots(db, musician_id)
    if not slots:
        # El músico no tiene franjas restringidas publicadas; disponibilidad abierta sujeta a confirmación en cotización.
        return

    day_slots = matching_slots(slots, event_date)
    if not day_slots:
        labels = sorted({DAY_LABELS[s.day_of_week] for s in slots})
        raise HTTPException(
            400,
            "El músico no está disponible ese día. "
            f"Días disponibles: {', '.join(labels)}.",
        )

    if not matching_slots(slots, event_date, start_time):
        windows = ", ".join(
            f"{format_time(s.start_time)}–{format_time(s.end_time)}" for s in day_slots
        )
        raise HTTPException(
            400,
            f"La hora está fuera de la disponibilidad del músico ({windows}).",
        )
