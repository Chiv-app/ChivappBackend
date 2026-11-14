"""Un líder no puede activar a un músico real sin su consentimiento."""

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.core.hashing import hash_password
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _musician(db: Session, *, published: bool = True) -> tuple[User, MusicianProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"m_{suffix}@test.com",
        fullname=f"Músico {suffix}",
        password_hash=hash_password("ClaveSegura-123!"),
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=f"Grupo {suffix}",
        slug=f"grupo-{suffix}",
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


def _add_member(client: TestClient, leader: User, target: User):
    _login(client, leader)
    return client.post(
        "/api/v1/musician/members",
        json={"email": target.email, "fullname": "Violinista Real", "specialties": ["Violín"]},
    )


def test_an_existing_musician_is_invited_not_activated(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()

    with patch("app.services.email.auth_emails.send_templated_email") as send:
        res = _add_member(client, leader, target)
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "invited"
    # Recibe un correo para responder, no un enlace para crear contraseña.
    assert send.call_args.kwargs["slug"] == "ensemble_join_request"

    _login(client, target)
    pending = client.get("/api/v1/musician/my-ensemble-invites").json()
    assert len(pending) == 1
    assert client.get("/api/v1/musician/my-ensembles").json() == []


def test_the_musician_accepts_and_then_can_leave(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]

    _login(client, target)
    accepted = client.post(f"/api/v1/musician/my-ensemble-invites/{member_id}/respond", json={"action": "accept"})
    assert accepted.status_code == 200 and accepted.json()["status"] == "active"
    assert [m["id"] for m in client.get("/api/v1/musician/my-ensembles").json()] == [member_id]

    left = client.post(f"/api/v1/musician/my-ensembles/{member_id}/leave")
    assert left.status_code == 200 and left.json()["status"] == "inactive"
    assert client.get("/api/v1/musician/my-ensembles").json() == []

    _login(client, leader)
    assert client.get(f"/api/v1/musician/members/{member_id}").json()["status"] == "inactive"


def test_the_musician_can_decline(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]

    _login(client, target)
    res = client.post(f"/api/v1/musician/my-ensemble-invites/{member_id}/respond", json={"action": "decline"})
    assert res.status_code == 200 and res.json()["status"] == "inactive"


def test_the_leader_cannot_force_a_member_to_active(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]

    _login(client, leader)
    res = client.patch(f"/api/v1/musician/members/{member_id}", json={"status": "active"})
    assert res.status_code == 400, res.text
    db_session.expire_all()
    assert db_session.get(EnsembleMember, uuid.UUID(member_id)).status == EnsembleMemberStatus.invited

    # Sí puede darlo de baja.
    assert client.patch(f"/api/v1/musician/members/{member_id}", json={"status": "inactive"}).status_code == 200


def test_nobody_can_leave_someone_elses_membership(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    stranger, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]
    _login(client, target)
    client.post(f"/api/v1/musician/my-ensemble-invites/{member_id}/respond", json={"action": "accept"})

    _login(client, stranger)
    assert client.post(f"/api/v1/musician/my-ensembles/{member_id}/leave").status_code == 404


def test_an_invited_real_musician_is_not_treated_as_member_only(client: TestClient, db_session: Session):
    """Un músico con perfil propio invitado a una agrupación sigue haciendo su propia guía."""
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session, published=False)
    db_session.commit()
    _add_member(client, leader, target)

    _login(client, target)
    assert client.get("/api/v1/auth/me").json()["is_ensemble_member"] is False


def test_the_leader_cannot_convoke_a_member_who_has_not_accepted(client: TestClient, db_session: Session):
    from datetime import time, timedelta
    from decimal import Decimal

    from app.core.timezone import now_peru
    from app.models.booking import Booking, BookingStatus
    from app.models.contractor_profile import ContractorProfile

    leader, profile = _musician(db_session)
    target, _ = _musician(db_session)
    cuser = User(
        email=f"c_{uuid.uuid4().hex[:8]}@test.com",
        fullname="Contratante",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(cuser)
    db_session.flush()
    contractor = ContractorProfile(user_id=cuser.id, city="Lima", document_type="DNI", document_number="12345678")
    db_session.add(contractor)
    db_session.flush()
    booking = Booking(
        musician_id=profile.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(18, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.commit()

    member_id = _add_member(client, leader, target).json()["id"]
    res = client.post(f"/api/v1/bookings/{booking.id}/member-invites", json={"ensemble_member_ids": [member_id]})
    assert res.status_code == 400, res.text
    assert "aceptó" in res.json()["detail"]
