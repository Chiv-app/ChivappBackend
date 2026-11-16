"""Calendario de la persona: eventos de todos sus perfiles, convocatorias, cruces y horas ocupadas en Google."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.calendar_service import BusyResult
from app.services.person_calendar import CalendarRange, MAX_RANGE_DAYS, subtract_synced_events

pytestmark = pytest.mark.db


def _musician(db: Session, name: str = "Carlos") -> tuple[User, MusicianProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"m_{suffix}@test.com",
        fullname=name,
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=f"{name} {suffix}",
        slug=f"{name.lower()}-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
    )
    db.add(profile)
    db.commit()
    return user, profile


def _contractor(db: Session) -> tuple[User, ContractorProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"c_{suffix}@test.com",
        fullname="Contratante",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = ContractorProfile(user_id=user.id, city="Lima", document_type="DNI", document_number=suffix)
    db.add(profile)
    db.commit()
    return user, profile


def _booking(
    db: Session,
    musician: MusicianProfile,
    contractor: ContractorProfile,
    *,
    day: date,
    start: time = time(18, 0),
    end: time | None = time(20, 0),
    status: BookingStatus = BookingStatus.payment_retained,
    synced: bool = False,
) -> Booking:
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=day,
        start_time=start,
        end_time=end,
        location_address="Jr. Ayacucho 123",
        location_city="Lima",
        event_type="Boda",
        price_agreed=Decimal("600.00"),
        status=status,
        calendar_event_id="evt-1" if synced else None,
    )
    db.add(booking)
    db.commit()
    return booking


def _login(client: TestClient, user: User) -> None:
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0))


def _events(client: TestClient, start: date, end: date):
    res = client.get("/api/v1/calendar/events", params={"start": start.isoformat(), "end": end.isoformat()})
    assert res.status_code == 200, res.text
    return res.json()["events"]


DAY = now_peru().date() + timedelta(days=10)
RANGE = (DAY - timedelta(days=3), DAY + timedelta(days=3))


def test_a_musician_sees_the_bookings_of_all_their_profiles_with_the_profile_name(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    other = MusicianProfile(
        user_id=user.id, is_primary=False, stage_name="Los Hermanos", slug="los-hermanos", status=ProfileStatus.published, availability_type=AvailabilityType.both
    )
    db_session.add(other)
    db_session.commit()
    _, contractor = _contractor(db_session)
    first = _booking(db_session, primary, contractor, day=DAY, start=time(10, 0), end=time(12, 0))
    second = _booking(db_session, other, contractor, day=DAY, start=time(18, 0), end=time(20, 0))
    _login(client, user)

    events = _events(client, *RANGE)

    assert {e["booking_id"] for e in events} == {str(first.id), str(second.id)}
    by_booking = {e["booking_id"]: e for e in events}
    assert by_booking[str(first.id)]["profile_name"] == primary.stage_name
    assert by_booking[str(second.id)]["profile_name"] == "Los Hermanos"
    assert by_booking[str(first.id)]["state"] == "confirmed"
    assert by_booking[str(first.id)]["start"] == f"{DAY.isoformat()}T10:00:00"
    assert not any(e["conflict"] for e in events)


def test_paid_bookings_are_confirmed_and_unpaid_ones_tentative_and_cancelled_ones_disappear(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _, contractor = _contractor(db_session)
    _booking(db_session, profile, contractor, day=DAY, start=time(9, 0), end=time(10, 0), status=BookingStatus.payment_retained)
    _booking(db_session, profile, contractor, day=DAY, start=time(11, 0), end=time(12, 0), status=BookingStatus.contract_signed)
    _booking(db_session, profile, contractor, day=DAY, start=time(13, 0), end=time(14, 0), status=BookingStatus.requested)
    _booking(db_session, profile, contractor, day=DAY, start=time(15, 0), end=time(16, 0), status=BookingStatus.cancelled)
    _login(client, user)

    states = {e["start"][11:16]: e["state"] for e in _events(client, *RANGE)}

    assert states == {"09:00": "confirmed", "11:00": "tentative", "13:00": "tentative"}


def test_events_outside_the_range_and_other_peoples_bookings_are_not_shown(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    stranger, stranger_profile = _musician(db_session, "Otro")
    _, contractor = _contractor(db_session)
    inside = _booking(db_session, profile, contractor, day=DAY)
    _booking(db_session, profile, contractor, day=DAY + timedelta(days=30))
    _booking(db_session, stranger_profile, contractor, day=DAY)
    _login(client, user)

    events = _events(client, *RANGE)

    assert [e["booking_id"] for e in events] == [str(inside.id)]
    assert stranger.id != user.id


def test_overlapping_events_are_flagged_as_conflicts_and_adjacent_ones_are_not(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _, contractor = _contractor(db_session)
    _booking(db_session, profile, contractor, day=DAY, start=time(18, 0), end=time(20, 0))
    _booking(db_session, profile, contractor, day=DAY, start=time(19, 0), end=time(21, 0), status=BookingStatus.requested)
    _booking(db_session, profile, contractor, day=DAY, start=time(21, 0), end=time(22, 0))
    _login(client, user)

    flags = {e["start"][11:16]: e["conflict"] for e in _events(client, *RANGE)}

    assert flags == {"18:00": True, "19:00": True, "21:00": False}


def test_a_booking_without_an_end_time_lasts_two_hours_by_default(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _, contractor = _contractor(db_session)
    _booking(db_session, profile, contractor, day=DAY, start=time(18, 0), end=None)
    _login(client, user)

    (event,) = _events(client, *RANGE)

    assert event["end"] == f"{DAY.isoformat()}T20:00:00"


def test_an_event_that_ends_after_midnight_runs_into_the_next_day(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _, contractor = _contractor(db_session)
    _booking(db_session, profile, contractor, day=DAY, start=time(22, 0), end=time(2, 0))
    _login(client, user)

    (event,) = _events(client, *RANGE)

    assert event["end"] == f"{(DAY + timedelta(days=1)).isoformat()}T02:00:00"


def test_convocations_appear_as_tentative_until_accepted(client: TestClient, db_session: Session):
    leader, leader_profile = _musician(db_session, "Lider")
    friend, friend_profile = _musician(db_session, "Amigo")
    _, contractor = _contractor(db_session)
    booking = _booking(db_session, leader_profile, contractor, day=DAY)
    member = EnsembleMember(
        leader_user_id=leader.id,
        profile_id=leader_profile.id,
        member_user_id=friend.id,
        fullname="Amigo",
        email=f"a_{uuid.uuid4().hex[:6]}@test.com",
        specialties=["Voz"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add(member)
    db_session.flush()
    invite = BookingMemberInvite(booking_id=booking.id, ensemble_member_id=member.id, status=BookingMemberInviteStatus.pending, response_token=uuid.uuid4().hex)
    db_session.add(invite)
    db_session.commit()
    _login(client, friend)

    (pending,) = _events(client, *RANGE)
    assert pending["kind"] == "convocation" and pending["role"] == "member"
    assert pending["state"] == "tentative"
    assert leader_profile.stage_name in pending["title"]

    invite.status = BookingMemberInviteStatus.accepted
    db_session.commit()
    (accepted,) = _events(client, *RANGE)
    assert accepted["state"] == "confirmed"

    invite.status = BookingMemberInviteStatus.declined
    db_session.commit()
    assert _events(client, *RANGE) == []
    assert friend_profile.id != leader_profile.id


def test_a_convocation_that_overlaps_a_own_booking_is_a_conflict(client: TestClient, db_session: Session):
    leader, leader_profile = _musician(db_session, "Lider")
    friend, friend_profile = _musician(db_session, "Amigo")
    _, contractor = _contractor(db_session)
    led = _booking(db_session, leader_profile, contractor, day=DAY, start=time(18, 0), end=time(20, 0))
    own = _booking(db_session, friend_profile, contractor, day=DAY, start=time(19, 0), end=time(21, 0))
    member = EnsembleMember(
        leader_user_id=leader.id, profile_id=leader_profile.id, member_user_id=friend.id, fullname="Amigo",
        email=f"a_{uuid.uuid4().hex[:6]}@test.com", specialties=["Voz"], status=EnsembleMemberStatus.active,
    )
    db_session.add(member)
    db_session.flush()
    db_session.add(BookingMemberInvite(booking_id=led.id, ensemble_member_id=member.id, status=BookingMemberInviteStatus.accepted, response_token=uuid.uuid4().hex))
    db_session.commit()
    _login(client, friend)

    events = _events(client, *RANGE)

    assert {e["booking_id"] for e in events} == {str(led.id), str(own.id)}
    assert all(e["conflict"] for e in events)


def test_a_contractor_sees_their_own_bookings_with_the_artist_name(client: TestClient, db_session: Session):
    _, profile = _musician(db_session)
    cuser, contractor = _contractor(db_session)
    booking = _booking(db_session, profile, contractor, day=DAY)
    _, other_contractor = _contractor(db_session)
    _booking(db_session, profile, other_contractor, day=DAY, start=time(10, 0), end=time(11, 0))
    _login(client, cuser)

    (event,) = _events(client, *RANGE)

    assert event["booking_id"] == str(booking.id)
    assert event["role"] == "contractor"
    assert profile.stage_name in event["title"]


def test_the_range_is_validated_and_the_calendar_needs_a_musician_or_contractor(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    start = DAY.isoformat()

    too_long = client.get("/api/v1/calendar/events", params={"start": start, "end": (DAY + timedelta(days=MAX_RANGE_DAYS + 1)).isoformat()})
    backwards = client.get("/api/v1/calendar/events", params={"start": start, "end": (DAY - timedelta(days=1)).isoformat()})
    assert too_long.status_code == 400 and backwards.status_code == 400

    admin = User(email="admin@test.com", fullname="Admin", role=UserRole.admin, is_active=True, email_verified_at=datetime.utcnow())
    db_session.add(admin)
    db_session.commit()
    _login(client, admin)
    assert client.get("/api/v1/calendar/events", params={"start": start, "end": start}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/calendar/events", params={"start": start, "end": start}).status_code == 401


def test_busy_hours_from_google_exclude_what_the_app_already_sent(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _, contractor = _contractor(db_session)
    _booking(db_session, profile, contractor, day=DAY, start=time(18, 0), end=time(20, 0), synced=True)
    _login(client, user)
    google = BusyResult(
        connected=True,
        busy=[
            {"start": f"{DAY.isoformat()}T18:00:00-05:00", "end": f"{DAY.isoformat()}T20:00:00-05:00"},  # la reserva de Chivapp
            {"start": f"{DAY.isoformat()}T09:00:00-05:00", "end": f"{DAY.isoformat()}T10:30:00-05:00"},  # algo personal
        ],
    )

    with patch("app.api.v1.endpoints.calendar_events.get_busy_blocks", return_value=google):
        res = client.get("/api/v1/calendar/busy", params={"start": RANGE[0].isoformat(), "end": RANGE[1].isoformat()})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["connected"] is True and body["needs_reconnect"] is False
    assert [b["start"][11:16] for b in body["busy"]] == ["09:00"]


def test_busy_hours_report_when_google_is_not_connected_or_must_be_reconnected(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    params = {"start": RANGE[0].isoformat(), "end": RANGE[1].isoformat()}

    not_connected = client.get("/api/v1/calendar/busy", params=params).json()
    assert not_connected == {"start": params["start"], "end": params["end"], "connected": False, "needs_reconnect": False, "busy": []}

    with patch("app.api.v1.endpoints.calendar_events.get_busy_blocks", return_value=BusyResult(connected=True, needs_reconnect=True)):
        reconnect = client.get("/api/v1/calendar/busy", params=params).json()
    assert reconnect["connected"] is True and reconnect["needs_reconnect"] is True and reconnect["busy"] == []


def test_subtracting_synced_events_ignores_malformed_blocks():
    events = [{"start": "2026-12-01T18:00:00", "end": "2026-12-01T20:00:00", "synced": True}]
    busy = [
        {"start": "2026-12-01T18:00:30-05:00", "end": "2026-12-01T20:00:00-05:00"},
        {"start": "basura", "end": "2026-12-01T20:00:00-05:00"},
        {"start": "2026-12-01T21:00:00-05:00", "end": "2026-12-01T22:00:00-05:00"},
    ]
    assert subtract_synced_events(busy, events) == [busy[2]]
    assert CalendarRange.parse(date(2026, 12, 1), date(2026, 12, 1)).end == date(2026, 12, 1)


def test_the_google_connection_asks_for_the_busy_hours_permission_too(client: TestClient, db_session: Session):
    from app.core.config import settings
    from app.services.calendar_service import get_busy_blocks

    user, _ = _musician(db_session)
    _login(client, user)
    with patch.object(settings, "GOOGLE_CLIENT_ID", "client-id"), patch.object(settings, "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost/cb"):
        res = client.get("/api/v1/calendar-auth/url")

    assert res.status_code == 200, res.text
    url = res.json()["auth_url"]
    assert "calendar.events" in url and "calendar.freebusy" in url

    # Si freebusy está desactivado en producción mientras Google revisa el scope, solo se pide calendar.events
    user.google_calendar_refresh_token = "fake-refresh-token"
    with (
        patch.object(settings, "GOOGLE_CLIENT_ID", "client-id"),
        patch.object(settings, "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost/cb"),
        patch.object(settings, "GOOGLE_CALENDAR_FREEBUSY_ENABLED", False),
    ):
        res_disabled = client.get("/api/v1/calendar-auth/url")
        busy_disabled = get_busy_blocks(user, datetime(2026, 12, 1, 0, 0), datetime(2026, 12, 2, 0, 0))

    url_disabled = res_disabled.json()["auth_url"]
    assert "calendar.events" in url_disabled and "calendar.freebusy" not in url_disabled
    assert busy_disabled.connected is True and busy_disabled.needs_reconnect is False and busy_disabled.busy == []


def test_a_paid_booking_is_sent_to_the_musicians_google_calendar_automatically(db_session: Session):
    from app.services.mercadopago_service import _confirm_booking_notifications_and_calendar

    user, profile = _musician(db_session)
    user.google_calendar_refresh_token = "enc:token"
    cuser, contractor = _contractor(db_session)
    booking = _booking(db_session, profile, contractor, day=DAY)

    with patch("app.services.calendar_service.sync_booking_calendar", return_value="evt-123") as sync, patch(
        "app.services.mercadopago_service.notify_booking_confirmed"
    ):
        _confirm_booking_notifications_and_calendar(db_session, booking)

    sync.assert_called_once_with(db_session, str(booking.id), False, [])
    assert booking.calendar_event_id == "evt-123"
    assert cuser.id != user.id


def test_a_failing_google_calendar_never_blocks_the_payment_confirmation(db_session: Session):
    from app.services.mercadopago_service import _confirm_booking_notifications_and_calendar

    user, profile = _musician(db_session)
    user.google_calendar_refresh_token = "enc:token"
    _, contractor = _contractor(db_session)
    booking = _booking(db_session, profile, contractor, day=DAY)

    with patch("app.services.calendar_service.sync_booking_calendar", side_effect=RuntimeError("Google caído")), patch(
        "app.services.calendar_service.create_booking_event", return_value=None
    ), patch("app.services.mercadopago_service.notify_booking_confirmed") as notified:
        _confirm_booking_notifications_and_calendar(db_session, booking)

    notified.assert_called_once()
    assert booking.calendar_event_id is None


def test_google_calendar_event_uses_the_booked_profiles_stage_name(db_session: Session):
    from app.services.calendar_service import sync_booking_calendar

    user, _ = _musician(db_session, "Carlos Quispe")
    group = MusicianProfile(
        user_id=user.id,
        is_primary=False,
        stage_name="Los Hermanos Quispe",
        slug="los-hermanos-quispe-cal",
        profile_type="group",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
    )
    db_session.add(group)
    db_session.commit()
    user.google_calendar_refresh_token = "fake-refresh-token"
    _, contractor = _contractor(db_session)
    booking = _booking(db_session, group, contractor, day=DAY)
    captured: list[dict] = []

    class _FakeEvents:
        def insert(self, **kwargs):
            captured.append(kwargs.get("body", {}))

            class _Exec:
                def execute(self_inner):
                    return {"id": "evt-group-1"}

            return _Exec()

    class _FakeService:
        def events(self):
            return _FakeEvents()

    with patch("app.services.calendar_service._get_calendar_service_oauth", return_value=_FakeService()):
        event_id = sync_booking_calendar(db_session, str(booking.id), include_contractor=False, ensemble_member_ids=[])

    assert event_id == "evt-group-1"
    assert len(captured) == 1
    assert "Los Hermanos Quispe" in captured[0]["summary"]
    assert "Los Hermanos Quispe" in captured[0]["description"]
