"""Consentimiento de integrantes, parte 2: reinvitación limitada, sin pistas de cuentas y migración."""

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.hashing import hash_password
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
from app.models.notification import Notification
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _musician(db: Session) -> tuple[User, MusicianProfile]:
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
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
    )
    db.add(profile)
    db.flush()
    return user, profile


def _contractor(db: Session) -> User:
    user = User(
        email=f"c_{uuid.uuid4().hex[:8]}@test.com",
        fullname="Contratante",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    return user


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


def test_a_musician_who_declined_cannot_be_reinvited_right_away(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]
    _login(client, target)
    client.post(f"/api/v1/musician/my-ensemble-invites/{member_id}/respond", json={"action": "decline"})

    res = _add_member(client, leader, target)
    assert res.status_code == 400, res.text
    assert "volver a invitarla" in res.json()["detail"]

    # Pasados 30 días sí puede.
    member = db_session.get(EnsembleMember, uuid.UUID(member_id))
    member.ended_at = datetime.utcnow() - timedelta(days=31)
    db_session.commit()
    assert _add_member(client, leader, target).status_code == 201


def test_a_member_removed_by_the_leader_can_be_added_again(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]
    _login(client, leader)
    assert client.delete(f"/api/v1/musician/members/{member_id}").status_code == 200
    assert _add_member(client, leader, target).status_code == 201


def test_a_musician_who_left_cannot_be_reinvited_right_away(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    target, _ = _musician(db_session)
    db_session.commit()
    member_id = _add_member(client, leader, target).json()["id"]
    _login(client, target)
    client.post(f"/api/v1/musician/my-ensemble-invites/{member_id}/respond", json={"action": "accept"})
    client.post(f"/api/v1/musician/my-ensembles/{member_id}/leave")

    assert _add_member(client, leader, target).status_code == 400


def test_a_contractor_email_looks_like_any_other_invitation(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    contractor = _contractor(db_session)
    db_session.commit()

    with patch("app.services.email.auth_emails.send_templated_email") as send:
        res = _add_member(client, leader, contractor)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "invited"
    # No se vincula ni se avisa a esa cuenta, y la respuesta no delata nada.
    assert body["member_user_id"] is None and body["has_password"] is False
    send.assert_not_called()
    member = db_session.get(EnsembleMember, uuid.UUID(body["id"]))
    assert member.member_user_id is None

    # Reenviar responde igual que siempre, sin enviar nada.
    with patch("app.services.email.auth_emails.send_templated_email") as send:
        again = client.post(f"/api/v1/musician/members/{body['id']}/resend-invite")
    assert again.status_code == 200, again.text
    send.assert_not_called()


def test_responses_do_not_reveal_whether_the_email_has_an_account(client: TestClient, db_session: Session):
    leader, _ = _musician(db_session)
    real, _ = _musician(db_session)
    db_session.commit()

    existing = _add_member(client, leader, real).json()
    _login(client, leader)
    brand_new = client.post(
        "/api/v1/musician/members",
        json={"email": f"nuevo_{uuid.uuid4().hex[:8]}@test.com", "fullname": "Persona Nueva", "specialties": ["Voz"]},
    ).json()
    for body in (existing, brand_new):
        assert body["has_password"] is False
        assert body["member_user_id"] is None
        assert body["status"] == "invited"


def test_the_member_cap_is_enforced(client: TestClient, db_session: Session, monkeypatch):
    import app.api.v1.endpoints.ensemble_members as endpoint

    monkeypatch.setattr(endpoint, "MAX_ENSEMBLE_MEMBERS", 1)
    leader, _ = _musician(db_session)
    first, _ = _musician(db_session)
    second, _ = _musician(db_session)
    db_session.commit()
    assert _add_member(client, leader, first).status_code == 201
    res = _add_member(client, leader, second)
    assert res.status_code == 400 and "máximo" in res.json()["detail"]


def test_the_migration_only_reconfirms_members_who_never_consented(db_session: Session):
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20261114_c3d4e5f6a7b9_ensemble_member_consent.py"
    )
    spec = importlib.util.spec_from_file_location("consent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    leader, leader_profile = _musician(db_session)

    def member_for(user: User, name: str) -> EnsembleMember:
        m = EnsembleMember(
            leader_user_id=leader.id,
            member_user_id=user.id,
            fullname=name,
            email=user.email,
            specialties=["Voz"],
            status=EnsembleMemberStatus.active,
            joined_at=datetime.utcnow(),
        )
        db_session.add(m)
        db_session.flush()
        return m

    # 1) músico real activado por el líder sin respuesta -> vuelve a invitado
    real_user, _ = _musician(db_session)
    unconsented = member_for(real_user, "sin consentir")
    # 2) aceptó él mismo (hay aviso al líder) -> sigue activo
    accepted_user, _ = _musician(db_session)
    accepted = member_for(accepted_user, "aceptó")
    db_session.add(
        Notification(
            user_id=leader.id,
            type="ensemble_member_joined",
            title="x",
            message="x",
            meta={"ensemble_member_id": str(accepted.id)},
        )
    )
    # 3) cuenta que solo existe como integrante -> sigue activa
    shadow_user, shadow_profile = _musician(db_session)
    shadow_profile.is_ensemble_only = True
    shadow = member_for(shadow_user, "cuenta del líder")
    # 4) ya aceptó una convocatoria a un evento -> sigue activo
    answered_user, _ = _musician(db_session)
    answered = member_for(answered_user, "respondió convocatoria")

    cuser = _contractor(db_session)
    contractor = ContractorProfile(user_id=cuser.id, city="Lima", document_type="DNI", document_number="87654321")
    db_session.add(contractor)
    db_session.flush()
    booking = Booking(
        musician_id=leader_profile.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(18, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.flush()
    db_session.add(
        BookingMemberInvite(
            booking_id=booking.id,
            ensemble_member_id=answered.id,
            status=BookingMemberInviteStatus.accepted,
            response_token=uuid.uuid4().hex,
        )
    )
    db_session.commit()

    db_session.execute(text(module.RECONFIRM_UNCONSENTED_MEMBERS_SQL))
    db_session.commit()
    db_session.expire_all()

    def status_of(member: EnsembleMember) -> EnsembleMemberStatus:
        return db_session.get(EnsembleMember, member.id).status

    assert status_of(unconsented) == EnsembleMemberStatus.invited
    assert status_of(accepted) == EnsembleMemberStatus.active
    assert status_of(shadow) == EnsembleMemberStatus.active
    assert status_of(answered) == EnsembleMemberStatus.active
