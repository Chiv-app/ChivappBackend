"""Correcciones de la auditoría del 13/11/2026: precios, usuarios reservados, integrantes dados de baja,
calendar-sync, correo de retiro y pagos en vuelo."""

import re
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

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

pytestmark = pytest.mark.db


def _user(db: Session, role: UserRole, *, verified: bool = True, **extra) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"u_{suffix}@test.com",
        fullname=f"Usuario {suffix}",
        role=role,
        is_active=True,
        is_verified=verified,
        email_verified_at=datetime.utcnow(),
        **extra,
    )
    db.add(user)
    db.flush()
    return user


def _musician(db: Session, *, published: bool = True) -> tuple[User, MusicianProfile]:
    user = _user(db, UserRole.musician)
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=f"Grupo {uuid.uuid4().hex[:6]}",
        slug=f"grupo-{uuid.uuid4().hex[:8]}",
        status=ProfileStatus.published if published else ProfileStatus.draft,
        availability_type=AvailabilityType.both,
    )
    db.add(profile)
    db.flush()
    return user, profile


def _login(client: TestClient, user: User) -> None:
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0),
    )


def _booking(db: Session, musician: MusicianProfile, status=BookingStatus.payment_retained) -> Booking:
    contractor_user = _user(db, UserRole.contractor, phone="999111222")
    contractor = ContractorProfile(
        user_id=contractor_user.id, city="Lima", document_type="DNI", document_number=uuid.uuid4().hex[:8]
    )
    db.add(contractor)
    db.flush()
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=status,
    )
    db.add(booking)
    db.flush()
    return booking


# ------------------------------------------------------------------ precios
@pytest.mark.parametrize("value", [0, -50, 5_000_000])
def test_invalid_prices_are_rejected(client: TestClient, db_session: Session, value):
    user, _ = _musician(db_session, published=False)
    db_session.commit()
    _login(client, user)
    res = client.put("/api/v1/profiles/musician", json={"price_per_event": value})
    assert res.status_code == 422, res.text


def test_a_valid_price_is_accepted(client: TestClient, db_session: Session):
    user, _ = _musician(db_session, published=False)
    db_session.commit()
    _login(client, user)
    res = client.put("/api/v1/profiles/musician", json={"price_per_event": 350})
    assert res.status_code == 200, res.text


# ------------------------------------------------------------------ usuarios reservados
def test_reserved_stage_names_get_a_different_handle(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, published=False)
    profile.stage_name = None
    profile.slug = None
    db_session.commit()
    _login(client, user)

    res = client.put("/api/v1/profiles/musician", json={"stage_name": "Chivapp"})
    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(User, user.id).username == "chivapp-2"


def test_reserved_username_cannot_be_chosen(client: TestClient, db_session: Session):
    user, _ = _musician(db_session, published=False)
    db_session.commit()
    _login(client, user)
    res = client.put("/api/v1/profiles/musician", json={"username": "admin"})
    assert res.status_code == 400, res.text


# ------------------------------------------------------------------ integrantes dados de baja
def test_removed_member_loses_access_to_the_booking_and_never_sees_money(client: TestClient, db_session: Session):
    leader, profile = _musician(db_session)
    member_user, _member_profile = _musician(db_session)
    booking = _booking(db_session, profile)
    member = EnsembleMember(
        leader_user_id=leader.id,
        member_user_id=member_user.id,
        fullname="Violinista",
        email=member_user.email,
        specialties=["Violín"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add(member)
    db_session.flush()
    db_session.add(
        BookingMemberInvite(
            booking_id=booking.id,
            ensemble_member_id=member.id,
            status=BookingMemberInviteStatus.accepted,
            response_token=uuid.uuid4().hex,
        )
    )
    db_session.commit()

    _login(client, member_user)
    seen = client.get(f"/api/v1/bookings/{booking.id}")
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["viewer_role"] == "member"
    assert body["price_agreed"] is None
    assert body["platform_fee_amount"] is None
    assert body["complaint_reason"] is None

    _login(client, leader)
    assert client.delete(f"/api/v1/musician/members/{member.id}").status_code == 200

    _login(client, member_user)
    assert client.get(f"/api/v1/bookings/{booking.id}").status_code in (403, 404)
    calls = client.get("/api/v1/musician/my-calls")
    assert calls.status_code == 200
    assert calls.json() == []


def test_the_leader_still_sees_the_money(client: TestClient, db_session: Session):
    leader, profile = _musician(db_session)
    booking = _booking(db_session, profile)
    db_session.commit()
    _login(client, leader)
    body = client.get(f"/api/v1/bookings/{booking.id}").json()
    assert body["viewer_role"] == "owner"
    assert float(body["price_agreed"]) == 600.0


def test_a_removed_member_no_longer_counts_as_ensemble_member(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    member_user, _ = _musician(db_session)
    member = EnsembleMember(
        leader_user_id=leader.id,
        member_user_id=member_user.id,
        fullname="Ex integrante",
        email=member_user.email,
        specialties=["Voz"],
        status=EnsembleMemberStatus.inactive,
    )
    db_session.add(member)
    db_session.commit()
    _login(client, member_user)
    assert client.get("/api/v1/auth/me").json()["is_ensemble_member"] is False

    member.status = EnsembleMemberStatus.active
    db_session.commit()
    assert client.get("/api/v1/auth/me").json()["is_ensemble_member"] is True


# ------------------------------------------------------------------ calendar-sync
def test_calendar_sync_rejects_bookings_that_are_not_paid(client: TestClient, db_session: Session):
    leader, profile = _musician(db_session)
    booking = _booking(db_session, profile, status=BookingStatus.requested)
    db_session.commit()
    _login(client, leader)
    res = client.post(f"/api/v1/bookings/{booking.id}/calendar-sync", json={"include_contractor": True})
    assert res.status_code == 400, res.text
    assert "pago" in res.json()["detail"].lower()


def test_ics_text_cannot_inject_extra_properties():
    from app.api.v1.endpoints.ensemble_members import _ics_text

    evil = "Boda\r\nATTENDEE:mailto:x@evil.test\nURL:https://evil.test, otro;"
    out = _ics_text(evil)
    assert "\r" not in out and "\n" not in out
    assert "\\," in out and "\\;" in out


def test_calendar_event_text_is_html_escaped():
    from app.services.calendar_service import _h

    assert _h('<a href="https://evil.test">clic</a>') == "&lt;a href=&quot;https://evil.test&quot;&gt;clic&lt;/a&gt;"
    assert _h(None) == ""


# ------------------------------------------------------------------ correo de retiro
def test_payout_completed_template_exists_and_renders_its_variables():
    from app.services.email.defaults import EMAIL_TEMPLATE_DEFAULTS

    template = next(t for t in EMAIL_TEMPLATE_DEFAULTS if t["slug"] == "musician_payout_completed")
    used = set(re.findall(r"{{\s*(\w+)\s*}}", template["html_body"] + template["text_body"] + template["subject"]))
    assert used <= set(template["available_variables"])
    assert "amount" in used and "user_name" in used


def test_every_template_slug_used_in_code_is_defined():
    from pathlib import Path

    from app.services.email.defaults import EMAIL_TEMPLATE_DEFAULTS

    defined = {t["slug"] for t in EMAIL_TEMPLATE_DEFAULTS}
    app_dir = Path(__file__).resolve().parents[2] / "app"
    used: set[str] = set()
    for path in app_dir.rglob("*.py"):
        used |= set(re.findall(r'slug="([a-z_0-9]+)"', path.read_text(encoding="utf-8")))
    # Solo cuentan los slugs que se envían como plantilla de correo (los de notificación no están aquí).
    missing = {slug for slug in used if slug.startswith(("booking_", "musician_", "payment_", "password_", "email_"))} - defined
    assert not missing, f"Plantillas usadas en el código y no definidas: {sorted(missing)}"


# ------------------------------------------------------------------ pago en vuelo
def test_in_flight_direct_payment_expires_after_ten_minutes():
    from app.api.v1.endpoints.payments import _is_live_in_flight_direct

    fresh = SimpleNamespace(gateway_metadata={"in_flight_direct": True}, created_at=datetime.utcnow())
    stale = SimpleNamespace(
        gateway_metadata={"in_flight_direct": True}, created_at=datetime.utcnow() - timedelta(minutes=11)
    )
    other = SimpleNamespace(gateway_metadata={}, created_at=datetime.utcnow())
    assert _is_live_in_flight_direct(fresh) is True
    assert _is_live_in_flight_direct(stale) is False
    assert _is_live_in_flight_direct(other) is False


# ------------------------------------------------------------------ PDF de contrato del músico
def test_musician_contract_pdf_goes_through_the_private_store(db_session: Session):
    from unittest.mock import patch

    from app.services.contract_pdf import _write_private_pdf_bytes

    user, _ = _musician(db_session)
    with patch("app.services.uploads._store_private") as store:
        url = _write_private_pdf_bytes("contract-musician-x", b"%PDF-1.4 test", owner_id=user.id, db=db_session)
    store.assert_called_once()
    filename, content, content_type = store.call_args.args
    assert filename.startswith("contract-musician-x-") and filename.endswith(".pdf")
    assert content == b"%PDF-1.4 test" and content_type == "application/pdf"
    assert url.endswith(filename)
