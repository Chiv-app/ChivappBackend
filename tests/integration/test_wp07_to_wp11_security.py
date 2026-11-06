"""Integration tests for Fase 2 Security packages (WP-07, WP-08, WP-09, WP-10, WP-11)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints.auth import reset_login_failure_state
from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.admin_audit_log import AdminAuditLog
from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.notification import Notification
from app.models.profile_status import ProfileStatus
from app.models.uploaded_file import UploadedFile
from app.models.user import User, UserRole
from app.services.calendar_service import decrypt_refresh_token, encrypt_refresh_token
from app.services.contract_pdf import render_contract_pdf_bytes


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def test_wp07_account_login_lockout_and_audit_log(client: TestClient, db_session: Session):
    reset_login_failure_state()
    suffix = uuid.uuid4().hex[:8]
    admin = User(
        email=f"admin_{suffix}@test.com",
        password_hash=hash_password("AdminPass123!"),
        role=UserRole.admin,
        fullname="Admin Test",
        is_verified=True,
        is_active=True,
    )
    target_user = User(
        email=f"user_{suffix}@test.com",
        password_hash=hash_password("UserPass123!"),
        role=UserRole.contractor,
        fullname="Target User",
        is_verified=False,
        is_active=True,
    )
    db_session.add_all([admin, target_user])
    db_session.commit()

    # 5 failed attempts on nonexistent or existing email trigger 429 on 6th attempt
    target_email = f"nonexistent_{suffix}@test.com"
    for _ in range(5):
        r = client.post(
            "/api/v1/auth/login",
            json={"email": target_email, "password": "WrongPassword123!"},
        )
        assert r.status_code == 401

    locked = client.post(
        "/api/v1/auth/login",
        json={"email": target_email, "password": "WrongPassword123!"},
    )
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers

    # Admin mutating action writes to admin_audit_log and is queryable via GET /admin/audit-logs
    headers = _auth_headers(admin)
    patch_res = client.patch(
        f"/api/v1/admin/users/{target_user.id}",
        headers=headers,
        json={"is_verified": True},
    )
    assert patch_res.status_code == 200

    logs_res = client.get(
        f"/api/v1/admin/audit-logs?target_id={target_user.id}",
        headers=headers,
    )
    assert logs_res.status_code == 200
    logs = logs_res.json()
    assert len(logs) >= 1
    assert logs[0]["action"] == "user.update"
    assert logs[0]["admin_email"] == admin.email
    assert logs[0]["before_state"]["is_verified"] is False
    assert logs[0]["after_state"]["is_verified"] is True


def test_wp07_payout_info_change_notifies_musician(client: TestClient, db_session: Session):
    suffix = uuid.uuid4().hex[:8]
    musician_user = User(
        email=f"mus_payout_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="Músico Payout",
        is_verified=True,
        is_active=True,
    )
    db_session.add(musician_user)
    db_session.flush()
    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name=f"Agrupación Payout {suffix}",
        slug=f"agrupacion-payout-{suffix}",
        status=ProfileStatus.published,
        payout_method="yape_plin",
        payout_phone="999111222",
    )
    db_session.add(musician)
    db_session.commit()

    res = client.put(
        "/api/v1/profiles/musician",
        headers=_auth_headers(musician_user),
        json={"payout_phone": "999888777"},
    )
    assert res.status_code == 200
    notif = (
        db_session.query(Notification)
        .filter(
            Notification.user_id == musician_user.id,
            Notification.type == "payout_info_updated",
        )
        .first()
    )
    assert notif is not None


def test_wp08_privacy_redaction_and_signature_restriction(client: TestClient, db_session: Session):
    suffix = uuid.uuid4().hex[:8]
    musician_user = User(
        email=f"mus_priv_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="Músico Priv",
        phone="911222333",
        is_verified=True,
        is_active=True,
    )
    contractor_user = User(
        email=f"con_priv_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.contractor,
        fullname="Contratista Priv",
        phone="944555666",
        is_verified=True,
        is_active=True,
    )
    db_session.add_all([musician_user, contractor_user])
    db_session.flush()

    sig_file = f"{uuid.uuid4().hex}.png"
    db_session.add(
        UploadedFile(
            filename=sig_file,
            owner_user_id=musician_user.id,
            content_type="image/png",
            size_bytes=120,
        )
    )
    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name=f"Artista Priv {suffix}",
        slug=f"artista-priv-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        signature_image_url=f"/uploads/private/{sig_file}",
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        document_type="DNI",
        document_number=f"7{suffix[:7]}",
        address="Calle Secreta 999",
        city="Lima",
        status=ProfileStatus.published,
    )
    db_session.add_all([musician, contractor])
    db_session.flush()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_type="Boda",
        event_date=date.today() + timedelta(days=20),
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Av. Evento 123",
        location_city="Lima",
        price_agreed=Decimal("800.00"),
        status=BookingStatus.accepted,
    )
    db_session.add(booking)
    db_session.flush()

    contract = Contract(
        booking_id=booking.id,
        title="Contrato Musical",
        body=(
            f"<p>Tel músico: {musician_user.phone} - Correo cliente: {contractor_user.email} "
            f"- DNI: {contractor.document_number} - Dir: {contractor.address}</p>"
        ),
        context={
            "musician_phone": musician_user.phone,
            "contractor_email": contractor_user.email,
            "contractor_document_number": contractor.document_number,
            "contractor_address": contractor.address,
        },
        musician_signed=True,
        contractor_signed=False,
        musician_signature_url=f"/uploads/private/{sig_file}",
    )
    db_session.add(contract)
    db_session.commit()

    # Before payment, counterparty phone/email are redacted in GET /contracts/booking/{id}
    c_res = client.get(
        f"/api/v1/contracts/booking/{booking.id}",
        headers=_auth_headers(contractor_user),
    )
    assert c_res.status_code == 200
    c_data = c_res.json()
    assert musician_user.phone not in c_data["body"]
    assert c_data["context"]["musician_phone"] == "[Visible tras confirmar el pago]"

    # Before contractor signs, musician cannot see contractor DNI/address
    m_res = client.get(
        f"/api/v1/contracts/booking/{booking.id}",
        headers=_auth_headers(musician_user),
    )
    assert m_res.status_code == 200
    m_data = m_res.json()
    assert contractor.document_number not in m_data["body"]
    assert m_data["context"]["contractor_document_number"] == "[Pendiente de aceptación del cliente]"

    # S-03: Counterparty cannot download signature image before BOTH parties have signed the contract
    sig_res = client.get(
        f"/uploads/private/{sig_file}",
        headers=_auth_headers(contractor_user),
    )
    assert sig_res.status_code in (403, 404)


def test_wp09_jti_revocation_on_logout_and_wp10_limits(client: TestClient, db_session: Session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"jti_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="JTI User",
        is_verified=True,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        MusicianProfile(
            user_id=user.id,
            stage_name=f"JTI Artist {suffix}",
            slug=f"jti-artist-{suffix}",
            status=ProfileStatus.draft,
        )
    )
    db_session.commit()

    headers = _auth_headers(user)
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    # WP-10: Oversized contract_template_body (>100_000 chars) rejected with 422
    huge_res = client.put(
        "/api/v1/profiles/musician",
        headers=headers,
        json={"contract_template_body": "<p>" + ("A" * 105_000) + "</p>"},
    )
    assert huge_res.status_code == 422

    # WP-09: Logout revokes token JTI immediately
    logout_res = client.post("/api/v1/auth/logout", headers=headers)
    assert logout_res.status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_wp11_domain_separated_fernet_and_pdf_ip_escaping():
    encrypted = encrypt_refresh_token("refresh-token-12345")
    assert encrypted.startswith("enc:v1:")
    assert decrypt_refresh_token(encrypted) == "refresh-token-12345"

    # Escaping sign_ip in PDF generation does not fail or inject raw HTML tags
    pdf_bytes = render_contract_pdf_bytes(
        title="Contrato Seguro",
        body="<p>Cláusula única de prueba con longitud suficiente para generar PDF.</p>",
        context={"{{nombre_artista}}": "Artista", "{{nombre_cliente}}": "Cliente"},
        signed_at=datetime.utcnow(),
        sign_ip='127.0.0.1"><script>alert(1)</script>',
    )
    assert pdf_bytes.startswith(b"%PDF")
