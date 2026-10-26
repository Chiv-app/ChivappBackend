"""Tests de regresión para la Fase 1 del Plan de Cierre (Auditoría v6): Cuentas y sesión."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from fastapi import HTTPException

from app.api.v1.endpoints.auth import (
    _compute_otp_hmac,
    _get_cookie_domain,
    _mask_email,
    generate_and_send_login_otp,
    verify_login_otp_or_raise,
)
from app.cli.create_admin import upsert_admin_user
from app.core.config import settings
from app.core.hashing import hash_password, verify_password
from app.core.jwt import create_access_token, create_magic_token
from app.db.session import SessionLocal
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.schemas.booking import MusicianBookingCreate
from app.services.email.auth_emails import (
    hash_token,
    issue_email_verification_token,
    send_email_verification,
)
from app.services.musician_booking import create_musician_booking
from app.services.uniqueness import normalize_phone


def _auth_cookie(client, user: User) -> None:
    tok = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    client.cookies.set("access_token", tok)


def test_sec6_03_pre_registered_account_invalidates_attacker_password_on_email_proof(client, db_session):
    """SEC6-03: Si un atacante pre-registra un correo ajeno con contraseña y sin verificar,
    cuando el dueño legítimo demuestra posesión del buzón (p. ej. vía OTP o magic link),
    la contraseña del atacante se invalida y su sesión previa queda revocada."""
    attacker_res = client.post(
        "/api/v1/auth/register",
        json={
            "email": "dueno.real@example.com",
            "password": "AtacantePassword123!",
            "fullname": "Atacante",
            "role": "contractor",
            "phone": "999000111",
            "accepted_terms": True,
        },
    )
    assert attacker_res.status_code == 201
    attacker_cookie = attacker_res.cookies.get("access_token")
    assert attacker_cookie is not None

    user = db_session.query(User).filter(User.email == "dueno.real@example.com").first()
    assert user is not None
    assert user.email_verified_at is None
    assert user.password_hash is not None

    # El dueño legítimo recibe un código OTP en su correo y lo canjea
    user.login_otp_hash = _compute_otp_hmac(user.id, "654321")
    user.login_otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    user.login_otp_attempts = 0
    db_session.commit()

    client.cookies.clear()
    otp_res = client.post(
        "/api/v1/auth/login-otp",
        json={"email": "dueno.real@example.com", "otp": "654321"},
    )
    assert otp_res.status_code == 200

    db_session.refresh(user)
    assert user.email_verified_at is not None
    assert user.email_verification_token is None
    assert user.password_hash is None

    # La contraseña que puso el atacante ya no sirve
    client.cookies.clear()
    bad_login = client.post(
        "/api/v1/auth/login",
        json={"email": "dueno.real@example.com", "password": "AtacantePassword123!"},
    )
    assert bad_login.status_code in (400, 401)

    # Y la cookie de sesión inicial del atacante fue revocada por rotación de token_version
    client.cookies.set("access_token", attacker_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_bug6_03_sec6_14_verify_email_with_or_without_session_and_password(client, db_session):
    """BUG6-03 / SEC6-14 / FE6-01:
    1) Verificar en otro dispositivo proporcionando la contraseña correcta conserva la contraseña.
    2) Verificar sin sesión y sin contraseña anula la contraseña sospechosa y envía reset."""
    # Caso 1: usuario legítimo verifica desde otro dispositivo enviando su contraseña
    user1 = User(
        email="movil.pc@example.com",
        fullname="Usuario Móvil",
        role=UserRole.contractor,
        password_hash=hash_password("MiClaveSegura123"),
        is_verified=False,
    )
    db_session.add(user1)
    db_session.flush()
    tok1 = issue_email_verification_token(user1)
    db_session.commit()

    client.cookies.clear()
    res1 = client.post(
        f"/api/v1/auth/verify-email/{tok1}",
        json={"password": "MiClaveSegura123"},
    )
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["email_verified"] is True
    assert body1["password_reset_sent"] is False

    # Puede iniciar sesión inmediatamente con su contraseña original
    login1 = client.post(
        "/api/v1/auth/login",
        json={"email": "movil.pc@example.com", "password": "MiClaveSegura123"},
    )
    assert login1.status_code == 200

    # Caso 2: pre-registro por tercero; el dueño verifica sin sesión y sin conocer la contraseña
    user2 = User(
        email="victima.verify@example.com",
        fullname="Víctima",
        role=UserRole.contractor,
        password_hash=hash_password("ClaveDelAtacante999"),
        is_verified=False,
    )
    db_session.add(user2)
    db_session.flush()
    tok2 = issue_email_verification_token(user2)
    db_session.commit()

    client.cookies.clear()
    res2 = client.post(f"/api/v1/auth/verify-email/{tok2}")
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["email_verified"] is True
    assert body2["password_reset_sent"] is True

    db_session.refresh(user2)
    assert user2.password_hash is None
    assert user2.password_reset_token is not None


def test_bug6_04_expired_verification_token_still_shows_verify_email_message_on_login(client, db_session):
    """BUG6-04: Con enlace de verificación expirado, el usuario sigue viendo que debe verificar su correo."""
    user = User(
        email="expirado.verify@example.com",
        fullname="Expirado",
        role=UserRole.contractor,
        password_hash=hash_password("ClaveCorrecta123"),
        is_verified=False,
        email_verified_at=None,
    )
    db_session.add(user)
    db_session.flush()
    raw_token = issue_email_verification_token(user)
    user.email_verification_expires_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    exp_res = client.post(f"/api/v1/auth/verify-email/{raw_token}")
    assert exp_res.status_code == 410
    assert "expir" in exp_res.json()["detail"].lower()

    # Al intentar iniciar sesión con la contraseña pre-registrada, sigue exigiendo verificar el correo
    login_res = client.post(
        "/api/v1/auth/login",
        json={"email": "expirado.verify@example.com", "password": "ClaveCorrecta123"},
    )
    assert login_res.status_code in (401, 403)
    assert "verificar tu correo" in login_res.json()["detail"].lower()


def test_sec6_02_atomic_otp_attempts_invalidates_after_5_failures(db_session):
    """SEC6-02: Dos sesiones independientes incrementan login_otp_attempts sin perder actualizaciones
    y al 5.º fallo el OTP queda invalidado."""
    user = User(
        email="otp.atomic@example.com",
        fullname="OTP User",
        role=UserRole.contractor,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
        login_otp_expires_at=datetime.utcnow() + timedelta(minutes=10),
        login_otp_attempts=0,
    )
    db_session.add(user)
    db_session.flush()
    user.login_otp_hash = _compute_otp_hmac(user.id, "123456")
    db_session.commit()

    # Dos sesiones independientes que habían cargado el objeto antes de fallar en serie
    s1 = SessionLocal()
    s2 = SessionLocal()
    try:
        u1 = s1.query(User).filter(User.id == user.id).first()
        u2 = s2.query(User).filter(User.id == user.id).first()
        assert u1.login_otp_attempts == 0
        assert u2.login_otp_attempts == 0

        with pytest.raises(HTTPException) as e1:
            verify_login_otp_or_raise(s1, u1, "000000")
        assert e1.value.status_code == 401

        with pytest.raises(HTTPException) as e2:
            verify_login_otp_or_raise(s2, u2, "000000")
        assert e2.value.status_code == 401
    finally:
        s1.close()
        s2.close()

    db_session.refresh(user)
    assert user.login_otp_attempts == 2

    # 3.º y 4.º fallo devuelven 401; el 5.º devuelve 429 e invalida el hash
    for expected_code in (401, 401, 429):
        with pytest.raises(HTTPException) as exc_info:
            verify_login_otp_or_raise(db_session, user, "000000")
        assert exc_info.value.status_code == expected_code

    db_session.refresh(user)
    assert user.login_otp_hash is None

    # El 6.º intento con el código correcto debe seguir rechazado
    with pytest.raises(HTTPException) as exc_valid:
        verify_login_otp_or_raise(db_session, user, "123456")
    assert exc_valid.value.status_code == 429


def test_sec6_09_musician_earnings_rejects_unverified_even_with_query_param(client, db_session):
    """SEC6-09: GET /payments/musician/earnings?_require_verified=false con músico no verificado devuelve 403."""
    user = User(
        email="unverified.musician@example.com",
        fullname="Músico Sin Verificar",
        role=UserRole.musician,
        is_verified=False,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        MusicianProfile(
            user_id=user.id,
            stage_name="Mariachi No Verificado",
            status=ProfileStatus.draft,
        )
    )
    db_session.commit()

    _auth_cookie(client, user)
    res = client.get("/api/v1/payments/musician/earnings?_require_verified=false")
    assert res.status_code == 403


def test_sec6_13_resend_verification_preserves_valid_token_and_enforces_hourly_cap(client, db_session):
    """SEC6-13: Reenviar verificación dentro del cooldown no invalida el token vigente y limita a 5/hora."""
    user = User(
        email="cooldown.verify@example.com",
        fullname="Cooldown Verify",
        role=UserRole.contractor,
        is_verified=False,
    )
    db_session.add(user)
    db_session.flush()

    first_token = issue_email_verification_token(user)
    user.email_verification_sent_count = 1
    user.email_verification_window_started_at = datetime.utcnow()
    db_session.commit()

    # Llamar a resend-verification dentro de los 10 min de cooldown NO reemplaza el token
    res = client.post(
        "/api/v1/auth/resend-verification",
        json={"email": "cooldown.verify@example.com"},
    )
    assert res.status_code == 200

    db_session.refresh(user)
    assert user.email_verification_token == hash_token(first_token)

    # Y si ya envió 5 en la última hora, send_email_verification devuelve False incluso sin cooldown
    user.email_verification_sent_at = datetime.utcnow() - timedelta(minutes=20)
    user.email_verification_sent_count = 5
    db_session.commit()
    assert send_email_verification(db_session, user) is False


def test_sec4_08_unverified_checkout_does_not_block_verified_user_dni(client, db_session):
    """SEC4-08 + [EJEC DNI-2]: Cuenta A sin verificar reclama DNI en checkout (se guarda en contrato,
    no en el índice único); Cuenta B verificada registra ese DNI sin 500 ni 400; un checkout posterior
    con ese DNI ya verificado devuelve 409."""
    m_user = User(
        email="musico.sec408@example.com",
        fullname="Músico",
        role=UserRole.musician,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(m_user)
    db_session.flush()
    musician = MusicianProfile(
        user_id=m_user.id,
        stage_name="Mariachi SEC408",
        status=ProfileStatus.published,
        signature_image_url="https://example.com/sig.png",
        contract_template_body="<p>" + ("Cláusula de servicio musical " * 5) + "</p>",
    )
    db_session.add(musician)
    db_session.flush()

    link1 = BookingLink(
        musician_id=musician.id,
        token="tok-sec408-1",
        price=Decimal("500.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    link2 = BookingLink(
        musician_id=musician.id,
        token="tok-sec408-2",
        price=Decimal("500.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db_session.add_all([link1, link2])
    db_session.commit()

    # Cuenta A (nueva, no verificada) hace checkout con el DNI 44556677
    res_a = client.post(
        "/api/v1/booking-links/tok-sec408-1/checkout",
        json={
            "fullname": "Cuenta A Sin Verificar",
            "email": "cuenta.a.unverified@example.com",
            "phone": "911222333",
            "document_number": "44556677",
            "event_date": "2030-08-15",
            "start_time": "20:00:00",
            "event_type": "Cumpleaños",
            "location_address": "Av. Arequipa 100",
            "location_city": "Lima",
        },
    )
    assert res_a.status_code == 200

    # Cuenta B (verificada) reclama su DNI 44556677 en su perfil sin sufrir 500 ni 400
    user_b = User(
        email="cuenta.b.verified@example.com",
        fullname="Cuenta B Verificada",
        role=UserRole.contractor,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(user_b)
    db_session.commit()

    client.cookies.clear()
    _auth_cookie(client, user_b)
    res_b = client.put(
        "/api/v1/profiles/contractor",
        json={"document_type": "DNI", "document_number": "44556677"},
    )
    assert res_b.status_code == 200
    assert res_b.json()["document_number"] == "44556677"

    # Ahora que Cuenta B verificada tiene el DNI, otro tercero en checkout recibe 409
    client.cookies.clear()
    res_c = client.post(
        "/api/v1/booking-links/tok-sec408-2/checkout",
        json={
            "fullname": "Tercero",
            "email": "tercero.sec408@example.com",
            "phone": "944555666",
            "document_number": "44556677",
            "event_date": "2030-08-16",
            "start_time": "20:00:00",
            "event_type": "Serenata",
            "location_address": "Av. Larco 200",
            "location_city": "Lima",
        },
    )
    assert res_c.status_code == 409


def test_sec4_10_bug5_09_login_step_one_otp_cooldown_and_hourly_limit(db_session):
    """SEC4-10 / BUG5-09: generate_and_send_login_otp aplica cooldown de 60s y tope de 5/hora."""
    user = User(
        email="otp.cooldown@example.com",
        fullname="OTP Cooldown",
        role=UserRole.contractor,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(user)
    db_session.commit()

    with patch("app.services.email.service.send_templated_email") as mock_send:
        otp1 = generate_and_send_login_otp(db_session, user)
        assert len(otp1) == 6
        first_hash = user.login_otp_hash
        assert mock_send.call_count == 1

        # Segunda llamada inmediata (< 60 s): no reenvía ni pisa el OTP vigente
        with pytest.raises(HTTPException) as exc1:
            generate_and_send_login_otp(db_session, user)
        assert exc1.value.status_code == 429
        assert user.login_otp_hash == first_hash
        assert mock_send.call_count == 1

        # Si ya alcanzó 5 envíos en la ventana de 1 hora, bloquea aunque hayan pasado > 60 s
        user.login_otp_last_sent_at = datetime.utcnow() - timedelta(seconds=120)
        user.login_otp_sent_count = 5
        db_session.commit()
        with pytest.raises(HTTPException) as exc2:
            generate_and_send_login_otp(db_session, user)
        assert exc2.value.status_code == 429
        assert mock_send.call_count == 1


def test_sec4_13_walk_in_guest_local_isolated_per_musician(db_session):
    """SEC4-13: Dos músicos crean clientes sin correo (@guest.local) con el mismo teléfono y DNI
    sin compartir cuenta de cliente."""
    long_contract = "<p>" + ("Cláusula de contrato musical válido " * 5) + "</p>"
    m1_user = User(email="m1.walkin@example.com", fullname="M1", role=UserRole.musician, is_verified=True)
    m2_user = User(email="m2.walkin@example.com", fullname="M2", role=UserRole.musician, is_verified=True)
    db_session.add_all([m1_user, m2_user])
    db_session.flush()

    m1 = MusicianProfile(
        user_id=m1_user.id,
        stage_name="Mariachi Uno Walkin",
        status=ProfileStatus.published,
        signature_image_url="https://example.com/s1.png",
        contract_template_body=long_contract,
    )
    m2 = MusicianProfile(
        user_id=m2_user.id,
        stage_name="Mariachi Dos Walkin",
        status=ProfileStatus.published,
        signature_image_url="https://example.com/s2.png",
        contract_template_body=long_contract,
    )
    db_session.add_all([m1, m2])
    db_session.commit()

    payload1 = MusicianBookingCreate(
        contractor_fullname="Cliente Walkin M1",
        contractor_email=None,
        contractor_phone="988777666",
        document_type="DNI",
        document_number="77889900",
        EventDate=None,
        event_date=date(2030, 9, 10),
        start_time=time(18, 0),
        end_time=time(19, 0),
        event_type="Boda",
        location_address="Jr. Unión 100",
        location_city="Lima",
        price_agreed=Decimal("400.00"),
    )
    payload2 = MusicianBookingCreate(
        contractor_fullname="Cliente Walkin M2",
        contractor_email=None,
        contractor_phone="988777666",
        document_type="DNI",
        document_number="77889900",
        event_date=date(2030, 9, 11),
        start_time=time(18, 0),
        end_time=time(19, 0),
        event_type="Aniversario",
        location_address="Jr. Unión 200",
        location_city="Lima",
        price_agreed=Decimal("450.00"),
    )

    b1 = create_musician_booking(db_session, musician=m1, musician_user=m1_user, payload=payload1)
    b2 = create_musician_booking(db_session, musician=m2, musician_user=m2_user, payload=payload2)

    assert b1.contractor_id != b2.contractor_id


def test_sec4_18_password_max_length_and_create_admin_refuses_silent_promotion(client, db_session):
    """SEC4-18: Contraseña > 128 caracteres devuelve 422 rápido; create_admin no promueve sin --promote."""
    res = client.post(
        "/api/v1/auth/register",
        json={
            "email": "longpass@example.com",
            "password": "A" * 200,
            "fullname": "Long Pass",
            "role": "contractor",
        },
    )
    assert res.status_code == 422

    musician_user = User(
        email="musico.no.admin@example.com",
        fullname="Músico",
        role=UserRole.musician,
    )
    db_session.add(musician_user)
    db_session.commit()

    with pytest.raises(PermissionError):
        upsert_admin_user(
            db_session,
            email="musico.no.admin@example.com",
            fullname="Admin",
            password="AdminPassword123",
            promote=False,
        )


def test_bug5_12_normalize_phone_equates_plus51_spaces_and_dashes():
    """BUG5-12: '999 111 222', '+51999111222' y '999-111-222' normalizan al mismo teléfono."""
    p1 = normalize_phone("999 111 222")
    p2 = normalize_phone("+51999111222")
    p3 = normalize_phone("999-111-222")
    assert p1 == "999111222"
    assert p1 == p2 == p3


def test_fe6_10_login_step_one_sends_otp_for_passwordless_unverified_user(client, db_session):
    """FE6-10: Cuenta sin contraseña y no verificada recibe status='otp_sent' en login-step-one."""
    user = User(
        email="guest.sin.clave@example.com",
        fullname="Guest Sin Clave",
        role=UserRole.contractor,
        password_hash=None,
        is_verified=False,
        email_verified_at=None,
    )
    db_session.add(user)
    db_session.commit()

    res = client.post(
        "/api/v1/auth/login-step-one",
        json={"email": "guest.sin.clave@example.com"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "otp_sent"


def test_sec3_14_magic_link_get_does_not_consume_post_consumes_once(client, db_session):
    """SEC3-14: GET /auth/magic-link/login no emite cookie ni gasta el token; POST lo canjea una sola vez."""
    user = User(
        email="magic.one.time@example.com",
        fullname="Magic One Time",
        role=UserRole.contractor,
        token_version=0,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_magic_token(str(user.id), "/bookings", version=0)

    # Un escáner de correo hace GET: no emite cookie ni consume el enlace
    get_res = client.get(f"/api/v1/auth/magic-link/login?token={token}", follow_redirects=False)
    assert get_res.status_code in (302, 307)
    assert "access_token" not in get_res.cookies
    assert "/magic-link?" in get_res.headers["location"]

    # El usuario confirma con POST: recibe cookie y el token queda invalidado para un 2.º uso
    post_res1 = client.post("/api/v1/auth/magic-link/login", json={"token": token})
    assert post_res1.status_code == 200
    assert "access_token" in post_res1.cookies
    assert post_res1.json()["redirect_to"] == "/bookings"

    client.cookies.clear()
    post_res2 = client.post("/api/v1/auth/magic-link/login", json={"token": token})
    assert post_res2.status_code == 401


def test_sec_02_03_22_23_schemas_masking_and_cookie_domain(monkeypatch):
    """SEC-02, SEC-03, SEC-22, SEC-23: límites de longitud, enmascarado de email y dominio de cookie."""
    from pydantic import ValidationError
    from app.schemas.booking_link import BookingLinkCheckout
    from app.schemas.email import EmailTemplateUpdate

    with pytest.raises(ValidationError):
        BookingLinkCheckout(
            event_date=date(2030, 8, 15),
            start_time=time(20, 0),
            event_type="Serenata",
            location_address="Av. Arequipa 100",
            location_city="C" * 150,
            fullname="Cliente",
            email="c@example.com",
            phone="999111222",
        )

    with pytest.raises(ValidationError):
        EmailTemplateUpdate(html_body="H" * 110_000)

    masked = _mask_email("cliente.secreto@example.com")
    assert "cliente.secreto" not in masked
    assert masked.endswith("@example.com")

    monkeypatch.setattr(settings, "FRONTEND_URL", "https://chiv.com.pe")
    assert _get_cookie_domain() is None
