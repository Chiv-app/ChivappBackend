"""Quien registra su cuenta con contraseña y vuelve a entrar con un código no pierde su contraseña.

La protección contra pre-registros ajenos (SEC6-03) se mantiene: solo se conserva la contraseña cuando la
petición viene del mismo dispositivo donde se creó la cuenta."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints.auth import _compute_otp_hmac
from app.core.jwt import create_magic_token
from app.models.user import User

pytestmark = pytest.mark.db

PASSWORD = "ClaveSegura-123!"


def _register_musician(client: TestClient, db: Session) -> tuple[str, User]:
    email = f"musico_{uuid.uuid4().hex[:8]}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "role": "musician", "accepted_terms": True},
    )
    assert res.status_code == 201, res.text
    user = db.query(User).filter(User.email == email).one()
    assert user.password_hash and user.email_verified_at is None
    return email, user


def _give_otp(db: Session, user: User, otp: str = "654321") -> None:
    user.login_otp_hash = _compute_otp_hmac(user.id, otp)
    user.login_otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    user.login_otp_attempts = 0
    db.commit()


def _login_with_code(client: TestClient, email: str, otp: str = "654321"):
    return client.post("/api/v1/auth/login-otp", json={"email": email, "otp": otp})


def test_registration_sets_a_device_proof_cookie(client: TestClient, db_session: Session):
    _register_musician(client, db_session)
    assert client.cookies.get("reg_proof")


def test_the_same_device_keeps_its_password_when_returning_with_a_code(client: TestClient, db_session: Session):
    email, user = _register_musician(client, db_session)
    _give_otp(db_session, user)

    # La sesión venció o se cerró la app, pero el dispositivo es el mismo.
    client.cookies.delete("access_token")
    res = _login_with_code(client, email)
    assert res.status_code == 200, res.text

    db_session.refresh(user)
    assert user.password_hash is not None
    assert user.email_verified_at is not None
    me = client.get("/api/v1/auth/me").json()
    assert me["has_password"] is True

    # Y la contraseña sigue sirviendo para entrar.
    client.cookies.clear()
    ok = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert ok.status_code == 200, ok.text


def test_the_proof_is_consumed_once_the_email_is_verified(client: TestClient, db_session: Session):
    email, user = _register_musician(client, db_session)
    _give_otp(db_session, user)
    client.cookies.delete("access_token")
    assert _login_with_code(client, email).status_code == 200
    assert not client.cookies.get("reg_proof")


def test_another_device_still_loses_the_pre_registered_password(client: TestClient, db_session: Session):
    """SEC6-03: sin la marca del dispositivo, entrar con código anula la contraseña pre-registrada."""
    email, user = _register_musician(client, db_session)
    _give_otp(db_session, user)

    client.cookies.clear()
    assert _login_with_code(client, email).status_code == 200

    db_session.refresh(user)
    assert user.password_hash is None
    assert user.email_verified_at is not None


def test_a_proof_from_another_account_does_not_help(client: TestClient, db_session: Session):
    victim_email, victim = _register_musician(client, db_session)  # pre-registrada por un tercero
    client.cookies.clear()
    _register_musician(client, db_session)  # el tercero queda con la marca de SU cuenta
    assert client.cookies.get("reg_proof")

    _give_otp(db_session, victim)
    client.cookies.delete("access_token")
    assert _login_with_code(client, victim_email).status_code == 200

    db_session.refresh(victim)
    assert victim.password_hash is None


def test_a_forged_proof_does_not_help(client: TestClient, db_session: Session):
    email, user = _register_musician(client, db_session)
    _give_otp(db_session, user)
    client.cookies.clear()
    client.cookies.set("reg_proof", "no-es-un-token-valido")
    assert _login_with_code(client, email).status_code == 200

    db_session.refresh(user)
    assert user.password_hash is None


def test_magic_link_on_the_same_device_keeps_the_password(client: TestClient, db_session: Session):
    email, user = _register_musician(client, db_session)
    token = create_magic_token(str(user.id), "/", user.token_version or 0)
    client.cookies.delete("access_token")
    res = client.post("/api/v1/auth/magic-link/login", json={"token": token})
    assert res.status_code == 200, res.text

    db_session.refresh(user)
    assert user.password_hash is not None
    assert user.email_verified_at is not None


def test_magic_link_on_another_device_clears_the_password(client: TestClient, db_session: Session):
    email, user = _register_musician(client, db_session)
    token = create_magic_token(str(user.id), "/", user.token_version or 0)
    client.cookies.clear()
    res = client.post("/api/v1/auth/magic-link/login", json={"token": token})
    assert res.status_code == 200, res.text

    db_session.refresh(user)
    assert user.password_hash is None
