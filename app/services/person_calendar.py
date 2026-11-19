"""Calendario de la persona: todo lo que tiene en agenda, con sus perfiles y convocatorias, y los cruces.

Un músico ve las reservas de todos sus perfiles y las convocatorias a eventos de otros grupos; un contratante,
sus propias reservas. Cada evento indica si está confirmado o es tentativo (aún sin pago) y si se cruza con otro.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import BookingMemberInvite, BookingMemberInviteStatus, EnsembleMember
from app.models.musician_profile import MusicianProfile
from app.models.user import User, UserRole
from app.services.availability_match import DEFAULT_BOOKING_DURATION_MINUTES
from app.services.musician_profiles import user_profile_ids

MAX_RANGE_DAYS = 62

# Pago retenido o más adelante, sin cancelar: la reserva está confirmada.
CONFIRMED_STATUSES = (
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
    BookingStatus.completed,
)
# Aún sin pago: se muestran como tentativas para no ofrecer esa hora a otro cliente sin querer.
TENTATIVE_STATUSES = (
    BookingStatus.requested,
    BookingStatus.accepted,
    BookingStatus.contract_pending,
    BookingStatus.contract_signed,
)

# Estados que ocupan la hora de verdad (para detectar cruces).
BUSY_STATES = {"confirmed", "tentative"}


@dataclass
class CalendarRange:
    start: date
    end: date

    @classmethod
    def parse(cls, start: date, end: date) -> "CalendarRange":
        if end < start:
            raise ValueError("La fecha final es anterior a la inicial.")
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValueError(f"El rango no puede superar {MAX_RANGE_DAYS} días.")
        return cls(start, end)


def _bounds(event_date: date, start: time, end: time | None) -> tuple[datetime, datetime]:
    begin = datetime.combine(event_date, start)
    if end is not None:
        finish = datetime.combine(event_date, end)
        if finish <= begin:
            finish += timedelta(days=1)
    else:
        finish = begin + timedelta(minutes=DEFAULT_BOOKING_DURATION_MINUTES)
    return begin, finish


def _state(booking: Booking) -> str | None:
    if booking.status in CONFIRMED_STATUSES:
        return "confirmed"
    if booking.status in TENTATIVE_STATUSES:
        return "tentative"
    return None  # cancelada u otro estado: no ocupa la agenda


def _booking_event(
    booking: Booking,
    *,
    kind: str,
    state: str,
    title: str,
    profile: MusicianProfile | None,
    role: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    from app.services.calendar_service import get_booking_calendar_event_id_for_viewer

    begin, finish = _bounds(booking.event_date, booking.start_time, booking.end_time)
    synced_id = get_booking_calendar_event_id_for_viewer(
        booking, role=role, user_id=user_id
    )
    return {
        "id": f"{kind}:{booking.id}",
        "booking_id": str(booking.id),
        "kind": kind,
        "role": role,
        "state": state,
        "title": title,
        "event_type": booking.event_type,
        "start": begin.isoformat(),
        "end": finish.isoformat(),
        "city": booking.location_city,
        "address": booking.location_address,
        "profile_id": str(profile.id) if profile else None,
        "profile_name": (profile.stage_name if profile else None),
        "synced": bool(synced_id),
        "conflict": False,
    }


def _mark_conflicts(events: list[dict[str, Any]]) -> None:
    """Marca los eventos que se pisan entre sí (solo confirmados o tentativos)."""
    spans = [
        (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]), e)
        for e in events
        if e["state"] in BUSY_STATES
    ]
    spans.sort(key=lambda s: s[0])
    for i, (a_start, a_end, a) in enumerate(spans):
        for b_start, b_end, b in spans[i + 1 :]:
            if b_start >= a_end:
                break
            if a_start < b_end and b_start < a_end:
                a["conflict"] = True
                b["conflict"] = True


def _musician_events(db: Session, user: User, rng: CalendarRange) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    profile_ids = user_profile_ids(db, user)
    if profile_ids:
        bookings = (
            db.query(Booking)
            .options(joinedload(Booking.musician), joinedload(Booking.contractor))
            .filter(
                Booking.musician_id.in_(profile_ids),
                Booking.event_date >= rng.start,
                Booking.event_date <= rng.end,
                Booking.status.in_(CONFIRMED_STATUSES + TENTATIVE_STATUSES),
            )
            .all()
        )
        for booking in bookings:
            state = _state(booking)
            if state is None:
                continue
            events.append(
                _booking_event(
                    booking,
                    kind="booking",
                    state=state,
                    title=booking.event_type or "Evento",
                    profile=booking.musician,
                    role="owner",
                )
            )

    convoked_query = (
        db.query(Booking, BookingMemberInvite)
        .join(BookingMemberInvite, BookingMemberInvite.booking_id == Booking.id)
        .join(EnsembleMember, EnsembleMember.id == BookingMemberInvite.ensemble_member_id)
        .options(joinedload(Booking.musician))
        .filter(
            EnsembleMember.member_user_id == user.id,
            BookingMemberInvite.status.in_([BookingMemberInviteStatus.pending, BookingMemberInviteStatus.accepted]),
            Booking.event_date >= rng.start,
            Booking.event_date <= rng.end,
            Booking.status.in_(CONFIRMED_STATUSES + TENTATIVE_STATUSES),
        )
    )
    if profile_ids:
        # Las reservas de los propios perfiles ya salieron arriba como propias.
        convoked_query = convoked_query.filter(Booking.musician_id.notin_(profile_ids))
    convoked = convoked_query.all()
    for booking, invite in convoked:
        base = _state(booking)
        if base is None:
            continue
        accepted = invite.status == BookingMemberInviteStatus.accepted
        group = booking.musician.stage_name if booking.musician else None
        events.append(
            _booking_event(
                booking,
                kind="convocation",
                # Una convocatoria sin responder es tentativa aunque la reserva ya esté pagada.
                state="confirmed" if (accepted and base == "confirmed") else "tentative",
                title=f"{booking.event_type or 'Evento'} con {group}" if group else (booking.event_type or "Evento"),
                profile=None,
                role="member",
                user_id=str(user.id),
            )
        )
    return events


def _contractor_events(db: Session, user: User, rng: CalendarRange) -> list[dict[str, Any]]:
    contractor = db.query(ContractorProfile).filter(ContractorProfile.user_id == user.id).first()
    if contractor is None:
        return []
    bookings = (
        db.query(Booking)
        .options(joinedload(Booking.musician))
        .filter(
            Booking.contractor_id == contractor.id,
            Booking.event_date >= rng.start,
            Booking.event_date <= rng.end,
            Booking.status.in_(CONFIRMED_STATUSES + TENTATIVE_STATUSES),
        )
        .all()
    )
    events: list[dict[str, Any]] = []
    for booking in bookings:
        state = _state(booking)
        if state is None:
            continue
        who = booking.musician.stage_name if booking.musician else None
        events.append(
            _booking_event(
                booking,
                kind="booking",
                state=state,
                title=f"{booking.event_type or 'Evento'} con {who}" if who else (booking.event_type or "Evento"),
                profile=None,
                role="contractor",
            )
        )
    return events


def list_calendar_events(db: Session, user: User, rng: CalendarRange) -> list[dict[str, Any]]:
    """Eventos de la persona en el rango, ordenados por inicio, con los cruces marcados."""
    if user.role == UserRole.musician:
        events = _musician_events(db, user, rng)
    elif user.role == UserRole.contractor:
        events = _contractor_events(db, user, rng)
    else:
        events = []
    _mark_conflicts(events)
    events.sort(key=lambda e: (e["start"], e["id"]))
    return events


def subtract_synced_events(busy: list[dict[str, str]], events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Quita de las horas ocupadas de Google las de eventos que la propia app ya envió (no se duplican)."""
    synced = [
        (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
        for e in events
        if e["synced"]
    ]
    kept = []
    for block in busy:
        try:
            start = datetime.fromisoformat(block["start"]).replace(tzinfo=None)
            end = datetime.fromisoformat(block["end"]).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        duplicate = any(abs((start - s).total_seconds()) < 90 and abs((end - e).total_seconds()) < 90 for s, e in synced)
        if not duplicate:
            kept.append(block)
    return kept
