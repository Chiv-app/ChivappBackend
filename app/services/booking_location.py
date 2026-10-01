"""Lógica de compartir ubicación en vivo (multi-participante)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.booking_location import (
    BookingLocationParticipant,
    BookingLocationPing,
    BookingLocationShare,
)
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    EnsembleMember,
)
from app.models.musician_profile import MusicianProfile
from app.models.user import User, UserRole
from app.schemas.booking_location import (
    LiveLocationCoords,
    LiveLocationEventPoint,
    LiveLocationParticipantOut,
    LiveLocationSessionOut,
)

ParticipantRole = Literal["leader", "member", "contractor"]

# Sin heartbeat reciente se considera la sesión cerrada (app cerrada / red caída).
STALE_SHARE_SECONDS = 90


def parse_event_coords(booking: Booking) -> tuple[float | None, float | None]:
    ref = (booking.location_reference or "").strip()
    if not ref:
        return None, None
    parts = [p.strip() for p in ref.split(",")]
    if len(parts) != 2:
        return None, None
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except ValueError:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, None
    return lat, lng


def assert_event_phase(db: Session, booking: Booking) -> None:
    if booking.status != BookingStatus.in_progress:
        raise HTTPException(
            400,
            "La ubicación en vivo solo está disponible en la fase de evento "
            "(después del pago).",
        )


def get_or_create_share(db: Session, booking: Booking) -> BookingLocationShare:
    share = (
        db.query(BookingLocationShare)
        .filter(BookingLocationShare.booking_id == booking.id)
        .first()
    )
    if share:
        if share.event_lat is None or share.event_lng is None:
            event_lat, event_lng = parse_event_coords(booking)
            share.event_lat = event_lat
            share.event_lng = event_lng
            share.event_address = booking.location_address
            share.event_city = booking.location_city
        return share

    event_lat, event_lng = parse_event_coords(booking)
    share = BookingLocationShare(
        booking_id=booking.id,
        event_lat=event_lat,
        event_lng=event_lng,
        event_address=booking.location_address,
        event_city=booking.location_city,
    )
    db.add(share)
    db.flush()
    return share


def _collaborator_specs(
    db: Session, booking: Booking
) -> list[tuple[UUID, ParticipantRole, str]]:
    """Usuarios autorizados a compartir en esta reserva (sin duplicados)."""
    by_user: dict[UUID, tuple[UUID, ParticipantRole, str]] = {}

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if musician:
        leader = db.query(User).filter(User.id == musician.user_id).first()
        if leader:
            by_user[leader.id] = (
                leader.id,
                "leader",
                leader.fullname or musician.stage_name or "Músico líder",
            )

    member_rows = (
        db.query(EnsembleMember, User)
        .join(
            BookingMemberInvite,
            BookingMemberInvite.ensemble_member_id == EnsembleMember.id,
        )
        .join(User, User.id == EnsembleMember.member_user_id)
        .filter(
            BookingMemberInvite.booking_id == booking.id,
            BookingMemberInvite.status == BookingMemberInviteStatus.accepted,
            EnsembleMember.member_user_id.isnot(None),
        )
        .all()
    )
    for member, user in member_rows:
        # El líder no se duplica como integrante.
        if user.id in by_user:
            continue
        by_user[user.id] = (
            user.id,
            "member",
            member.fullname or user.fullname or "Integrante",
        )

    contractor = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if contractor:
        contractor_user = (
            db.query(User).filter(User.id == contractor.user_id).first()
        )
        if contractor_user:
            by_user[contractor_user.id] = (
                contractor_user.id,
                "contractor",
                contractor_user.fullname or "Contratista",
            )

    return list(by_user.values())


def resolve_participant_role(
    db: Session, booking: Booking, user: User
) -> ParticipantRole:
    for user_id, role, _name in _collaborator_specs(db, booking):
        if user_id == user.id:
            return role
    raise HTTPException(403, "No autorizado para ubicación en vivo")


# Compat alias usado por endpoints / notificaciones.
def resolve_party(db: Session, booking: Booking, user: User) -> ParticipantRole:
    return resolve_participant_role(db, booking, user)


def ensure_participants(
    db: Session, booking: Booking
) -> list[BookingLocationParticipant]:
    from sqlalchemy.exc import IntegrityError

    get_or_create_share(db, booking)
    specs = _collaborator_specs(db, booking)
    allowed_ids = {user_id for user_id, _role, _name in specs}

    existing = {
        row.user_id: row
        for row in db.query(BookingLocationParticipant)
        .filter(BookingLocationParticipant.booking_id == booking.id)
        .all()
    }

    for user_id, role, display_name in specs:
        row = existing.get(user_id)
        if row:
            row.role = role
            row.display_name = display_name
            continue

        row = BookingLocationParticipant(
            booking_id=booking.id,
            user_id=user_id,
            role=role,
            display_name=display_name,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            existing[user_id] = row
        except IntegrityError:
            # Carrera concurrente: otra request ya creó la fila.
            recovered = (
                db.query(BookingLocationParticipant)
                .filter(
                    BookingLocationParticipant.booking_id == booking.id,
                    BookingLocationParticipant.user_id == user_id,
                )
                .first()
            )
            if recovered:
                recovered.role = role
                recovered.display_name = display_name
                existing[user_id] = recovered

    for user_id, row in list(existing.items()):
        if user_id not in allowed_ids and row.sharing:
            row.sharing = False
            row.lat = None
            row.lng = None
            row.accuracy = None

    db.flush()
    return [existing[uid] for uid, _role, _name in specs if uid in existing]


def get_participant(
    db: Session, booking: Booking, user: User
) -> BookingLocationParticipant:
    ensure_participants(db, booking)
    row = (
        db.query(BookingLocationParticipant)
        .filter(
            BookingLocationParticipant.booking_id == booking.id,
            BookingLocationParticipant.user_id == user.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(403, "No autorizado para ubicación en vivo")
    return row


def _log_ping(
    db: Session,
    *,
    booking: Booking,
    user: User,
    party: str,
    action: str,
    coords: LiveLocationCoords | None = None,
) -> None:
    db.add(
        BookingLocationPing(
            booking_id=booking.id,
            user_id=user.id,
            party=party,
            action=action,
            lat=coords.lat if coords else None,
            lng=coords.lng if coords else None,
            accuracy=coords.accuracy if coords else None,
        )
    )


def expire_stale_shares(db: Session, booking: Booking) -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=STALE_SHARE_SECONDS)
    rows = (
        db.query(BookingLocationParticipant)
        .filter(
            BookingLocationParticipant.booking_id == booking.id,
            BookingLocationParticipant.sharing.is_(True),
        )
        .all()
    )
    for row in rows:
        if row.updated_at is None or row.updated_at < cutoff:
            row.sharing = False
            row.lat = None
            row.lng = None
            row.accuracy = None
            row.updated_at = datetime.utcnow()
            db.add(
                BookingLocationPing(
                    booking_id=booking.id,
                    user_id=row.user_id,
                    party=row.role,
                    action="stale_stop",
                )
            )
    db.flush()


def serialize_session(
    db: Session,
    booking: Booking,
    *,
    viewer: User,
) -> LiveLocationSessionOut:
    share = get_or_create_share(db, booking)
    expire_stale_shares(db, booking)
    participants = ensure_participants(db, booking)
    me_row = next((p for p in participants if p.user_id == viewer.id), None)
    if not me_row:
        raise HTTPException(403, "No autorizado para ubicación en vivo")

    # Cualquier colaborador ve a quienes están compartiendo activamente.
    outs: list[LiveLocationParticipantOut] = []
    for row in participants:
        is_me = row.user_id == viewer.id
        visible = bool(
            row.sharing and row.lat is not None and row.lng is not None
        )
        outs.append(
            LiveLocationParticipantOut(
                user_id=row.user_id,
                role=row.role,
                display_name=row.display_name,
                is_me=is_me,
                sharing=bool(row.sharing),
                lat=row.lat if visible else None,
                lng=row.lng if visible else None,
                accuracy=row.accuracy if visible else None,
                updated_at=row.updated_at if visible else None,
                visible=visible,
                pending_request=bool(
                    is_me and row.requested_at is not None and not row.sharing
                ),
                requested_by_me=bool(
                    not is_me
                    and row.requested_at is not None
                    and row.requested_by_user_id == viewer.id
                    and not row.sharing
                ),
            )
        )

    me_out = next(p for p in outs if p.is_me)
    sharing_count = sum(1 for p in outs if p.sharing)

    return LiveLocationSessionOut(
        booking_id=booking.id,
        sharing_count=sharing_count,
        event=LiveLocationEventPoint(
            lat=share.event_lat,
            lng=share.event_lng,
            address=share.event_address,
            city=share.event_city,
        ),
        me=me_out,
        participants=outs,
    )


def start_sharing(
    db: Session,
    *,
    booking: Booking,
    user: User,
    coords: LiveLocationCoords,
) -> LiveLocationSessionOut:
    assert_event_phase(db, booking)
    row = get_participant(db, booking, user)
    now = datetime.utcnow()
    already = row.sharing
    had_request = row.requested_at is not None

    row.sharing = True
    row.lat = coords.lat
    row.lng = coords.lng
    row.accuracy = coords.accuracy
    row.updated_at = now
    row.requested_at = None
    row.requested_by_user_id = None

    if already:
        action = "update"
    elif had_request:
        action = "accept"
    else:
        action = "share_start"
    _log_ping(
        db, booking=booking, user=user, party=row.role, action=action, coords=coords
    )
    db.flush()
    return serialize_session(db, booking, viewer=user)


def update_position(
    db: Session,
    *,
    booking: Booking,
    user: User,
    coords: LiveLocationCoords,
    action: str = "update",
) -> LiveLocationSessionOut:
    assert_event_phase(db, booking)
    row = get_participant(db, booking, user)
    if not row.sharing:
        raise HTTPException(
            400,
            "Primero debes activar el compartir ubicación",
        )

    now = datetime.utcnow()
    row.lat = coords.lat
    row.lng = coords.lng
    row.accuracy = coords.accuracy
    row.updated_at = now
    _log_ping(
        db,
        booking=booking,
        user=user,
        party=row.role,
        action=action if action in {"update", "refresh"} else "update",
        coords=coords,
    )
    db.flush()
    return serialize_session(db, booking, viewer=user)


def stop_sharing(
    db: Session,
    *,
    booking: Booking,
    user: User,
) -> LiveLocationSessionOut:
    assert_event_phase(db, booking)
    row = get_participant(db, booking, user)
    row.sharing = False
    row.lat = None
    row.lng = None
    row.accuracy = None
    row.updated_at = datetime.utcnow()
    _log_ping(db, booking=booking, user=user, party=row.role, action="stop")
    db.flush()
    return serialize_session(db, booking, viewer=user)


def request_others(
    db: Session,
    *,
    booking: Booking,
    user: User,
) -> LiveLocationSessionOut:
    """Solicita ubicación a todos los colaboradores que aún no comparten."""
    assert_event_phase(db, booking)
    me = get_participant(db, booking, user)
    now = datetime.utcnow()
    targets = (
        db.query(BookingLocationParticipant)
        .filter(
            BookingLocationParticipant.booking_id == booking.id,
            BookingLocationParticipant.user_id != user.id,
            BookingLocationParticipant.sharing.is_(False),
        )
        .all()
    )
    if not targets:
        raise HTTPException(400, "Todos los participantes ya están compartiendo")

    for row in targets:
        row.requested_at = now
        row.requested_by_user_id = user.id

    _log_ping(db, booking=booking, user=user, party=me.role, action="request")
    db.flush()
    return serialize_session(db, booking, viewer=user)


# Compat name for older imports.
def request_other(
    db: Session,
    *,
    booking: Booking,
    user: User,
) -> LiveLocationSessionOut:
    return request_others(db, booking=booking, user=user)


def get_session(
    db: Session,
    *,
    booking: Booking,
    user: User,
) -> LiveLocationSessionOut:
    assert_event_phase(db, booking)
    get_participant(db, booking, user)
    return serialize_session(db, booking, viewer=user)


def list_notify_targets(
    db: Session,
    booking: Booking,
    *,
    exclude_user_id: UUID,
) -> list[User]:
    specs = _collaborator_specs(db, booking)
    users: list[User] = []
    for user_id, _role, _name in specs:
        if user_id == exclude_user_id:
            continue
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            users.append(u)
    return users
