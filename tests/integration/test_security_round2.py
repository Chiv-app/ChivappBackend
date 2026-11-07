"""Segunda ronda de seguridad (06-07/11/2026): bloqueo por IP, auditoría y datos personales."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints.auth import reset_login_failure_state
from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.admin_audit_log import AdminAuditLog
from app.models.user import User, UserRole

pytestmark = pytest.mark.db

ATTACKER_IP = "203.0.113.9"
ADMIN_IP = "198.51.100.7"


def _admin(db: Session, *, password: str = "AdminPass123!") -> User:
    suffix = uuid.uuid4().hex[:8]
    admin = User(
        email=f"admin_r2_{suffix}@test.com",
        password_hash=hash_password(password),
        role=UserRole.admin,
        fullname="Admin Ronda 2",
        is_verified=True,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    return admin


def test_failed_logins_from_one_ip_cannot_lock_the_real_owner_out(client: TestClient, db_session: Session):
    """Quien falla contraseñas se bloquea a sí mismo, no al dueño de la cuenta desde otra IP."""
    reset_login_failure_state()
    admin = _admin(db_session)

    for _ in range(5):
        res = client.post(
            "/api/v1/auth/login",
            json={"email": admin.email, "password": "ClaveIncorrecta123!"},
            headers={"X-Forwarded-For": ATTACKER_IP},
        )
        assert res.status_code == 401

    blocked = client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "ClaveIncorrecta123!"},
        headers={"X-Forwarded-For": ATTACKER_IP},
    )
    assert blocked.status_code == 429

    # El atacante sigue bloqueado aunque acierte la contraseña desde su IP...
    still_blocked = client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "AdminPass123!"},
        headers={"X-Forwarded-For": ATTACKER_IP},
    )
    assert still_blocked.status_code == 429

    # ...pero el administrador real, desde otra IP, entra con normalidad.
    ok = client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "AdminPass123!"},
        headers={"X-Forwarded-For": ADMIN_IP},
    )
    assert ok.status_code == 200, ok.text


def test_admin_actions_are_audited_with_the_client_ip(client: TestClient, db_session: Session):
    admin = _admin(db_session)
    token = create_access_token(subject=str(admin.id), role="admin", version=admin.token_version or 0)
    headers = {"Cookie": f"access_token={token}", "X-Forwarded-For": ADMIN_IP}

    res = client.patch(
        "/api/v1/admin/email/templates/welcome",
        json={"subject": "Bienvenido a Chivapp"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    db_session.expire_all()
    entry = (
        db_session.query(AdminAuditLog)
        .filter(AdminAuditLog.action == "email_template.update", AdminAuditLog.admin_user_id == admin.id)
        .order_by(AdminAuditLog.created_at.desc())
        .first()
    )
    assert entry is not None
    assert entry.target_id == "welcome"
    assert entry.ip_address == ADMIN_IP
    # Se guarda qué campos cambiaron, no el contenido completo de la plantilla.
    assert entry.after_data == {"fields_changed": ["subject"]}


def test_login_flow_answers_are_identical_for_real_missing_inactive_and_admin_accounts(
    client: TestClient, db_session: Session
):
    """S-04: el primer paso del acceso y la verificación del código no delatan qué cuentas existen."""
    from unittest.mock import patch

    reset_login_failure_state()
    suffix = uuid.uuid4().hex[:8]
    real = User(
        email=f"real_{suffix}@test.com",
        password_hash=hash_password("RealPass123!"),
        role=UserRole.contractor,
        fullname="Cuenta Real",
        is_verified=True,
        is_active=True,
    )
    inactive = User(
        email=f"inactivo_{suffix}@test.com",
        password_hash=hash_password("RealPass123!"),
        role=UserRole.contractor,
        fullname="Cuenta Inactiva",
        is_verified=True,
        is_active=False,
    )
    admin = _admin(db_session)
    db_session.add_all([real, inactive])
    db_session.commit()

    emails = {
        "real": real.email,
        "inexistente": f"nadie_{suffix}@test.com",
        "desactivada": inactive.email,
        "admin": admin.email,
    }
    with patch("app.services.email.service.send_templated_email") as mock_send:
        step_one = {
            name: client.post("/api/v1/auth/login-step-one", json={"email": email})
            for name, email in emails.items()
        }
        # Solo la cuenta real, activa y que no es de admin recibe un código.
        assert mock_send.call_count == 1
        assert mock_send.call_args.kwargs["to"] == real.email

    bodies = {name: (res.status_code, res.json()) for name, res in step_one.items()}
    assert len(set(str(v) for v in bodies.values())) == 1, bodies
    assert next(iter(bodies.values())) == (200, {"status": "otp_sent", "message": None})

    # Con un código equivocado, todas responden lo mismo (mismo estado y mismo texto).
    wrong = {
        name: client.post("/api/v1/auth/login-otp", json={"email": email, "otp": "000000"})
        for name, email in emails.items()
    }
    answers = {name: (res.status_code, res.json()["detail"]) for name, res in wrong.items()}
    assert len(set(answers.values())) == 1, answers
    assert next(iter(answers.values())) == (401, "Código inválido o expirado")

    # El bloqueo por intentos se activa igual para un correo inexistente que para uno real.
    for _ in range(4):
        client.post("/api/v1/auth/login-otp", json={"email": emails["inexistente"], "otp": "000000"})
    blocked_missing = client.post("/api/v1/auth/login-otp", json={"email": emails["inexistente"], "otp": "000000"})
    for _ in range(4):
        client.post("/api/v1/auth/login-otp", json={"email": emails["real"], "otp": "000000"})
    blocked_real = client.post("/api/v1/auth/login-otp", json={"email": emails["real"], "otp": "000000"})
    assert blocked_missing.status_code == blocked_real.status_code == 429


def test_unverified_email_cannot_sign_or_pay_until_it_is_verified_with_a_code(
    client: TestClient, db_session: Session
):
    """S-05: una cuenta nueva del checkout no firma ni paga hasta verificar el correo con un código."""
    from datetime import datetime, timedelta
    from decimal import Decimal
    from unittest.mock import patch

    from app.models.booking_link import BookingLink, BookingLinkStatus
    from app.models.contract import Contract
    from app.models.musician_profile import MusicianProfile
    from app.models.profile_status import ProfileStatus

    reset_login_failure_state()
    suffix = uuid.uuid4().hex[:8]
    m_user = User(
        email=f"musico_{suffix}@test.com",
        fullname="Músico S05",
        role=UserRole.musician,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(m_user)
    db_session.flush()
    musician = MusicianProfile(
        user_id=m_user.id,
        stage_name=f"Grupo S05 {suffix}",
        status=ProfileStatus.published,
        signature_image_url="https://example.com/sig.png",
        contract_template_body="<p>" + ("Cláusula de servicio musical " * 5) + "</p>",
    )
    db_session.add(musician)
    db_session.flush()
    link = BookingLink(
        musician_id=musician.id,
        token=f"tok-s05-{suffix}",
        price=Decimal("500.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db_session.add(link)
    db_session.commit()

    email = f"cliente_{suffix}@test.com"
    res = client.post(
        f"/api/v1/booking-links/{link.token}/checkout",
        json={
            "fullname": "Cliente Nuevo",
            "email": email,
            "phone": "955" + str(uuid.uuid4().int)[:6],
            "event_date": "2030-08-15",
            "start_time": "20:00:00",
            "event_type": "Cumpleaños",
            "location_address": "Av. Arequipa 100",
            "location_city": "Lima",
            "signature_image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
            "accept_terms": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["email_verification_required"] is True
    assert body["signature_image_url"] is None

    db_session.expire_all()
    contract = db_session.query(Contract).filter(Contract.booking_id == body["booking_id"]).first()
    assert contract is not None and not contract.contractor_signed

    # Con la sesión de esa cuenta sin verificar, pagar y firmar está bloqueado.
    pay = client.post("/api/v1/payments/mercadopago/preference", json={"booking_id": str(body["booking_id"])})
    assert pay.status_code == 403
    assert "verificar tu correo" in pay.json()["detail"]
    process = client.post("/api/v1/payments/mercadopago/process", json={"booking_id": str(body["booking_id"])})
    assert process.status_code in (403, 422)

    # Un código equivocado no verifica; el correcto sí y la sesión sigue activa.
    with patch("secrets.randbelow", return_value=123456), patch(
        "app.services.email.service.send_templated_email"
    ):
        sent = client.post("/api/v1/auth/email-verification/send-code")
    assert sent.status_code == 200 and sent.json()["email_verified"] is False

    bad = client.post("/api/v1/auth/email-verification/confirm", json={"otp": "000000"})
    assert bad.status_code == 401

    ok = client.post("/api/v1/auth/email-verification/confirm", json={"otp": "123456"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["email_verified"] is True

    user = db_session.query(User).filter(User.email == email).first()
    db_session.refresh(user)
    assert user.email_verified_at is not None
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200


def test_s09_booking_inputs_and_search_filters_have_size_limits(client: TestClient):
    """S-09: los textos libres y las listas de filtros tienen tope."""
    from datetime import date, time
    from pydantic import ValidationError

    from app.schemas.booking import BookingCreate

    base = {
        "musician_id": str(uuid.uuid4()),
        "event_date": date(2030, 1, 1),
        "start_time": time(20, 0),
        "location_address": "Av. Arequipa 100",
        "event_type": "Cumpleaños",
    }
    BookingCreate(**base)  # lo normal sigue pasando
    for field, too_long in (
        ("event_description", "x" * 2001),
        ("location_address", "x" * 256),
        ("event_type", "x" * 121),
    ):
        with pytest.raises(ValidationError):
            BookingCreate(**{**base, field: too_long})

    assert client.post("/api/v1/musicians/search", json={"genres": ["rock"] * 16}).status_code == 422
    assert client.post("/api/v1/musicians/search", json={"city": "x" * 121}).status_code == 422
    assert client.post("/api/v1/musicians/search", json={"instruments": ["y" * 61]}).status_code == 422
    assert client.post("/api/v1/musicians/search", json={"genres": ["rock"]}).status_code == 200


def test_s09_oversized_chunked_body_is_rejected(client: TestClient):
    """Una petición sin Content-Length (chunked) que supera el máximo se corta con 413."""

    def body():
        chunk = b"a" * (512 * 1024)
        for _ in range(5):  # 2.5 MB > 2 MB de límite para JSON
            yield chunk

    res = client.post(
        "/api/v1/auth/login-step-one",
        content=body(),
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413, res.text


def test_s09_search_pages_are_cut_in_the_database(client: TestClient, db_session: Session):
    from datetime import datetime
    from decimal import Decimal

    from app.models.musician_profile import MusicianProfile
    from app.models.profile_status import ProfileStatus

    suffix = uuid.uuid4().hex[:6]
    city = f"Ciudad{suffix}"
    created = []
    for i in range(3):
        user = User(
            email=f"m{i}_{suffix}@test.com",
            fullname=f"Músico {i}",
            role=UserRole.musician,
            is_verified=True,
            is_active=True,
            email_verified_at=datetime.utcnow(),
        )
        db_session.add(user)
        db_session.flush()
        db_session.add(
            MusicianProfile(
                user_id=user.id,
                stage_name=f"Grupo {suffix} {i}",
                slug=f"grupo-{suffix}-{i}",
                status=ProfileStatus.published,
                location_city=city,
                price_per_event=Decimal("300.00"),
            )
        )
        created.append(user)
    # Un cuarto músico publicado pero sin tarifa no debe aparecer.
    no_price = User(
        email=f"np_{suffix}@test.com",
        fullname="Sin tarifa",
        role=UserRole.musician,
        is_verified=True,
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(no_price)
    db_session.flush()
    db_session.add(
        MusicianProfile(
            user_id=no_price.id,
            stage_name=f"Sin tarifa {suffix}",
            slug=f"sin-tarifa-{suffix}",
            status=ProfileStatus.published,
            location_city=city,
        )
    )
    db_session.commit()

    page1 = client.post("/api/v1/musicians/search", json={"city": city, "limit": 2, "skip": 0}).json()
    page2 = client.post("/api/v1/musicians/search", json={"city": city, "limit": 2, "skip": 2}).json()
    ids = [r["musician_id"] for r in page1] + [r["musician_id"] for r in page2]
    assert len(page1) == 2 and len(page2) == 1
    assert len(set(ids)) == 3
    assert all("Sin tarifa" not in r["stage_name"] for r in page1 + page2)


def test_s09_small_chunked_body_still_works(client: TestClient):
    def body():
        yield b'{"email": "alguien@'
        yield b'test.com"}'

    res = client.post(
        "/api/v1/auth/login-step-one",
        content=body(),
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "otp_sent"
