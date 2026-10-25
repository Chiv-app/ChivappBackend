"""Pruebas de regresión para los hallazgos restantes de 01-seguridad-backend.md:
SEC-05, SEC-06, SEC-10, SEC-11, SEC-14, SEC-16, SEC-17, SEC-18, SEC-19, SEC-20, SEC-21, SEC-22, SEC-28."""

from datetime import date, datetime, time
from unittest.mock import patch

import pytest

from app.core.jwt import create_access_token, create_magic_token, create_oauth_pending_token
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import EnsembleMember
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.calendar_service import sync_booking_calendar
from app.services.html_sanitizer import sanitize_contract_html

pytestmark = pytest.mark.db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n"


def _login(client, user: User):
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(str(user.id), user.role.value, version=user.token_version or 0),
    )


def test_sec05_set_password_without_token_requires_verified_email(client, db_session):
    """SEC-05: Cuenta sombra sin email_verified_at no puede fijar contraseña sin token."""
    res = client.post(
        "/api/v1/auth/guest-register",
        json={"email": "shadow.sec05@example.com", "fullname": "Invitado", "phone": "911222333"},
    )
    assert res.status_code == 201
    # Intentar set-password con la cookie de invitado (email_verified_at is None)
    set_res = client.post(
        "/api/v1/auth/set-password",
        json={"password": "PasswordFuerte1!"},
    )
    assert set_res.status_code == 403
    assert "verificar tu correo" in set_res.json()["detail"].lower()

    # Y change-password tampoco permite crear contraseña sin email verificado
    chg_res = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "PasswordFuerte1!"},
    )
    assert chg_res.status_code == 403


def test_sec05_oauth_complete_respects_email_verified_flag(client, db_session):
    """SEC-05: Si el proveedor OAuth reporta email_verified=False, oauth_complete no marca email_verified_at."""
    pending = create_oauth_pending_token(
        {
            "provider": "google",
            "provider_user_id": "google-unverified-123",
            "email": "unverified.oauth@example.com",
            "fullname": "OAuth User",
            "picture_url": None,
            "email_verified": False,
        }
    )
    client.cookies.set("oauth_pending", pending)
    res = client.post(
        "/api/v1/auth/oauth/complete",
        json={"role": "contractor", "phone": "922333444"},
    )
    assert res.status_code == 200
    user = db_session.query(User).filter(User.email == "unverified.oauth@example.com").first()
    assert user is not None
    assert user.email_verified_at is None
    assert user.is_verified is False


def test_sec06_magic_link_respects_token_version_and_active_status(client, db_session):
    """SEC-06: Magic link incluye versión de sesión y rechaza tokens revocados o usuarios inactivos."""
    user = User(
        email="magic.sec06@example.com",
        fullname="Magic User",
        role=UserRole.contractor,
        token_version=2,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    stale_token = create_magic_token(str(user.id), "/contractor/bookings", version=1)
    res = client.get(f"/api/v1/auth/magic-link/login?token={stale_token}", follow_redirects=False)
    assert "error=invalid_magic_link" in res.headers["location"]
    assert "access_token" not in res.cookies

    valid_token = create_magic_token(str(user.id), "/contractor/bookings", version=2)
    res_ok = client.get(f"/api/v1/auth/magic-link/login?token={valid_token}", follow_redirects=False)
    assert res_ok.headers["location"].endswith("/contractor/bookings")
    assert "access_token" in res_ok.cookies


def test_sec10_sec18_musician_contract_pdf_sanitizes_html_and_stores_privately(client, db_session):
    """SEC-10 y SEC-18: El PDF de plantilla del músico escapa HTML y se guarda en /uploads/private/."""
    user = User(
        email="musico.pdf@example.com",
        fullname="Juan <script>alert(1)</script>",
        role=UserRole.musician,
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name="Mariachi <b>Real</b>",
        contract_template_title="Contrato <i>Especial</i>",
        contract_template_body=(
            "<p>" + ("Cláusula de servicio musical profesional para eventos. " * 3) + "{{nombre_artista}}</p>"
        ),
        status=ProfileStatus.published,
    )
    db_session.add(profile)
    db_session.commit()

    _login(client, user)
    res = client.post("/api/v1/profiles/musician/contract/generate")
    assert res.status_code == 200, res.text
    pdf_url = res.json()["contract_pdf_url"]
    assert pdf_url.startswith("/uploads/private/")

    # Otro usuario sin relación no puede descargar el PDF privado del músico
    stranger = User(email="stranger.pdf@example.com", fullname="Extraño", role=UserRole.contractor)
    db_session.add(stranger)
    db_session.commit()
    _login(client, stranger)
    assert client.get(pdf_url).status_code == 404


def test_sec11_login_does_not_leak_passwordless_account_existence(client, db_session):
    """SEC-11: Login en cuenta sin password devuelve el mismo 401 genérico que credenciales inválidas."""
    user = User(email="sinclave@example.com", fullname="Sin Clave", role=UserRole.contractor, password_hash=None)
    db_session.add(user)
    db_session.commit()

    res = client.post("/api/v1/auth/login", json={"email": "sinclave@example.com", "password": "CualquierPassword1!"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Credenciales inválidas"


def test_sec14_ensemble_invite_hashes_token_and_hides_invite_url(client, db_session):
    """SEC-14: El token de invitación de integrante se guarda con hash SHA-256, no expone invite_url al líder y no verifica al usuario."""
    leader = User(
        email="lider.sec14@example.com",
        fullname="Líder Mariachi",
        role=UserRole.musician,
        is_verified=True,
    )
    db_session.add(leader)
    db_session.flush()
    db_session.add(
        MusicianProfile(
            user_id=leader.id,
            stage_name="Mariachi Líder",
            slug="mariachi-lider",
            status=ProfileStatus.published,
        )
    )
    db_session.commit()

    _login(client, leader)
    res = client.post(
        "/api/v1/musician/members",
        json={
            "email": "integrante.nuevo@example.com",
            "fullname": "Integrante Nuevo",
            "phone": "933444555",
            "specialties": ["Trompeta"],
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["invite_url"] is None

    member = db_session.query(EnsembleMember).filter(EnsembleMember.email == "integrante.nuevo@example.com").first()
    assert member is not None
    assert member.password_setup_token is not None
    assert len(member.password_setup_token) == 64  # SHA-256 hex

    invited_user = db_session.get(User, member.member_user_id)
    assert invited_user is not None
    assert invited_user.is_verified is False


def test_sec16_sanitizer_blocks_private_uploads_and_dni_is_protected_from_counterparty(client, db_session):
    """SEC-16:
    1) sanitize_contract_html rechaza <img src="/uploads/private/...">.
    2) La contraparte de una reserva no puede descargar el DNI (id_document_url) del músico.
    3) No se puede vincular en el perfil un archivo privado ajeno."""
    cleaned = sanitize_contract_html('<p>Contrato</p><img src="/uploads/private/ajeno.png">')
    assert "/uploads/private/" not in cleaned

    musician_user = User(email="musico.dni@example.com", fullname="Músico DNI", role=UserRole.musician)
    contractor_user = User(email="cliente.dni@example.com", fullname="Cliente DNI", role=UserRole.contractor)
    db_session.add_all([musician_user, contractor_user])
    db_session.flush()

    _login(client, musician_user)
    up_res = client.post(
        "/api/v1/uploads/?private=true",
        files={"file": ("dni.pdf", PDF, "application/pdf")},
    )
    assert up_res.status_code == 200
    dni_url = up_res.json()["url"]

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi DNI",
        id_document_url=dni_url,
    )
    contractor = ContractorProfile(user_id=contractor_user.id)
    db_session.add_all([musician, contractor])
    db_session.flush()
    db_session.add(
        Booking(
            contractor_id=contractor.id,
            musician_id=musician.id,
            event_date=date(2030, 5, 10),
            start_time=time(19, 0),
            location_address="Av. Sol 123",
            event_type="Serenata",
            status=BookingStatus.contract_pending,
        )
    )
    db_session.commit()

    # El dueño sí puede ver su DNI
    assert client.get(dni_url).status_code == 200

    # La contraparte de la reserva NO puede descargar el DNI del músico
    _login(client, contractor_user)
    assert client.get(dni_url).status_code == 404

    # Y tampoco puede asignarse el archivo privado del músico como su propio id_document_url
    steal_res = client.put(
        "/api/v1/profiles/contractor",
        json={"id_document_url": dni_url},
    )
    assert steal_res.status_code == 400


def test_sec17_calendar_sync_filters_ensemble_members_by_leader(db_session):
    """SEC-17: sync_booking_calendar solo incluye integrantes cuyo leader_user_id es el músico de la reserva."""
    m1_user = User(email="m1.cal@example.com", fullname="M1", role=UserRole.musician)
    m2_user = User(email="m2.cal@example.com", fullname="M2", role=UserRole.musician)
    c_user = User(email="c.cal@example.com", fullname="Cliente", role=UserRole.contractor)
    db_session.add_all([m1_user, m2_user, c_user])
    db_session.flush()

    m1 = MusicianProfile(user_id=m1_user.id, stage_name="Grupo 1")
    c = ContractorProfile(user_id=c_user.id)
    db_session.add_all([m1, c])
    db_session.flush()

    own_member = EnsembleMember(
        leader_user_id=m1_user.id,
        email="propio.cal@example.com",
        fullname="Propio",
    )
    foreign_member = EnsembleMember(
        leader_user_id=m2_user.id,
        email="ajeno.cal@example.com",
        fullname="Ajeno",
    )
    db_session.add_all([own_member, foreign_member])
    db_session.flush()

    booking = Booking(
        contractor_id=c.id,
        musician_id=m1.id,
        event_date=date(2030, 6, 1),
        start_time=time(20, 0),
        location_address="Calle 1",
        event_type="Boda",
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.commit()

    # Sin token de Google Calendar retorna None, pero verificamos interceptando _get_calendar_service_oauth
    m1_user.google_calendar_refresh_token = "fake-refresh-token"
    db_session.commit()

    captured_bodies = []

    class _FakeEvents:
        def insert(self, **kwargs):
            captured_bodies.append(kwargs.get("body", {}))

            class _Exec:
                def execute(self_inner):
                    return {"id": "evt-123"}

            return _Exec()

    class _FakeService:
        def events(self):
            return _FakeEvents()

    with patch("app.services.calendar_service._get_calendar_service_oauth", return_value=_FakeService()):
        sync_booking_calendar(
            db_session,
            booking.id,
            include_contractor=False,
            ensemble_member_ids=[str(own_member.id), str(foreign_member.id)],
        )

    assert len(captured_bodies) == 1
    emails = [a["email"] for a in captured_bodies[0].get("attendees", [])]
    assert "propio.cal@example.com" in emails
    assert "ajeno.cal@example.com" not in emails


def test_sec20_public_profiles_and_reviews_reject_excessive_limit(client):
    """SEC-20: limit > 50 o <= 0 devuelve 422 en endpoints públicos de perfiles y reseñas."""
    assert client.get("/api/v1/profiles/musicians?limit=5000").status_code == 422
    assert client.get("/api/v1/profiles/musicians?skip=-1").status_code == 422
    assert client.get("/api/v1/profiles/reviews/latest?limit=5000").status_code == 422


def test_sec21_admin_settlements_csv_neutralizes_formulas(client, db_session):
    """SEC-21: El CSV de liquidaciones antepone comilla simple a celdas que inician con =, +, -, @."""
    admin = User(email="admin.csv@example.com", fullname="Admin", role=UserRole.admin, is_verified=True)
    musician_user = User(email="musico.csv@example.com", fullname="=cmd|' /C calc'!A0", role=UserRole.musician)
    contractor_user = User(email="cliente.csv@example.com", fullname="@malicioso", role=UserRole.contractor)
    db_session.add_all([admin, musician_user, contractor_user])
    db_session.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name="=1+1",
        payout_beneficiary_name="+SUM(1,2)",
    )
    contractor = ContractorProfile(user_id=contractor_user.id)
    db_session.add_all([musician, contractor])
    db_session.flush()

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=date(2030, 7, 1),
        start_time=time(18, 0),
        location_address="Av. CSV",
        event_type="-Evento",
        price_agreed=500.0,
        platform_fee_percent=6.0,
        platform_fee_amount=30.0,
        status=BookingStatus.completed,
    )
    db_session.add(booking)
    db_session.flush()
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=500.0,
            status=PaymentStatus.released,
        )
    )
    db_session.commit()

    _login(client, admin)
    res = client.get("/api/v1/admin/settlements/export")
    assert res.status_code == 200
    text = res.content.decode("utf-8-sig")
    assert "'=1+1" in text
    assert "'+SUM(1,2)" in text
    assert "'@malicioso" in text
    assert "'-Evento" in text


def test_sec28_admin_cancel_disables_public_share_token(client, db_session):
    """SEC-28: Al cancelar una reserva desde admin se desactiva el link público de compartir."""
    admin = User(email="admin.sec28@example.com", fullname="Admin", role=UserRole.admin, is_verified=True)
    musician_user = User(email="m.sec28@example.com", fullname="Músico", role=UserRole.musician)
    contractor_user = User(email="c.sec28@example.com", fullname="Cliente", role=UserRole.contractor)
    db_session.add_all([admin, musician_user, contractor_user])
    db_session.flush()

    musician = MusicianProfile(user_id=musician_user.id, stage_name="Mariachi Sec28")
    contractor = ContractorProfile(user_id=contractor_user.id)
    db_session.add_all([musician, contractor])
    db_session.flush()

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=date(2030, 8, 1),
        start_time=time(20, 0),
        location_address="Av. Sec28",
        event_type="Boda",
        price_agreed=400.0,
        status=BookingStatus.contract_pending,
        share_token="tok-sec28-activo",
        share_enabled=True,
    )
    db_session.add(booking)
    db_session.commit()

    _login(client, admin)
    res = client.post(f"/api/v1/admin/bookings/{booking.id}/cancel", json={"reason": "Cancelado por admin"})
    assert res.status_code == 200, res.text
    db_session.refresh(booking)
    assert booking.share_enabled is False
    assert booking.share_token is None


def test_sec2_01_mercadopago_webhook_runs_rate_limit_check_without_import_error(client):
    """SEC2-01: El webhook de Mercado Pago ejecuta _check_webhook_rate_limit usando app.core.limiter sin ImportError."""
    res = client.post(
        "/api/v1/payments/mercadopago/webhook",
        json={"type": "topic_merchant_order_wh", "data": {"id": "123"}},
    )
    assert res.status_code == 200
    assert res.json().get("status") == "ignored"


def test_sec2_02_admin_payout_request_process_and_reject_succeed_and_notify_musician(client, db_session):
    """SEC2-02: process y reject de solicitudes de retiro aceptan body AdminPayoutRequestAction y notifican al músico."""
    from decimal import Decimal
    from app.models.notification import Notification
    from app.models.payout_request import PayoutRequest

    admin = User(email="admin.sec202@example.com", fullname="Admin", role=UserRole.admin, is_verified=True)
    m_user = User(email="m.sec202@example.com", fullname="Músico", role=UserRole.musician, is_verified=True)
    db_session.add_all([admin, m_user])
    db_session.flush()
    musician = MusicianProfile(user_id=m_user.id, stage_name="Mariachi Retiros")
    db_session.add(musician)
    db_session.flush()

    req1 = PayoutRequest(musician_id=musician.id, amount=Decimal("300.00"), status="pending")
    db_session.add(req1)
    db_session.commit()

    _login(client, admin)
    res_proc = client.post(
        f"/api/v1/admin/payout-requests/{req1.id}/process",
        json={"notes": "Transferido BCP OP-999"},
    )
    assert res_proc.status_code == 200, res_proc.text
    assert res_proc.json()["status"] == "processed"

    req2 = PayoutRequest(musician_id=musician.id, amount=Decimal("250.00"), status="pending")
    db_session.add(req2)
    db_session.commit()

    res_rej = client.post(
        f"/api/v1/admin/payout-requests/{req2.id}/reject",
        json={"notes": "Cuenta CCI inválida"},
    )
    assert res_rej.status_code == 200, res_rej.text
    assert res_rej.json()["status"] == "rejected"

    notifs = db_session.query(Notification).filter(Notification.user_id == m_user.id).all()
    assert len(notifs) == 2


def test_sec2_03_sec2_05_sec2_14_payout_flow_completion_and_late_payment_alert(client, db_session):
    """SEC2-03, SEC2-05, SEC2-14:
    1) Al existir reseña del cliente y recomendación del músico, la reserva pasa a completed y es liquidable.
    2) request-payout notifica a los admins sin TypeError y bloquea duplicados.
    3) Pago tardío de Mercado Pago sobre reserva cancelada alerta a admins una sola vez."""
    from datetime import timedelta
    from decimal import Decimal
    from app.core.timezone import now_peru
    from app.models.booking import BookingReview
    from app.models.contractor_recommendation import ContractorRecommendation
    from app.models.notification import Notification
    from app.services.mercadopago_service import process_approved_mercadopago_payment
    from app.services.platform_payment import contractor_payable_total

    admin = User(email="admin.sec203@example.com", fullname="Admin", role=UserRole.admin, is_verified=True)
    m_user = User(email="m.sec203@example.com", fullname="Músico", role=UserRole.musician, is_verified=True)
    c_user = User(email="c.sec203@example.com", fullname="Cliente", role=UserRole.contractor, is_verified=True)
    db_session.add_all([admin, m_user, c_user])
    db_session.flush()

    musician = MusicianProfile(user_id=m_user.id, stage_name="Mariachi Sec203", status=ProfileStatus.published)
    contractor = ContractorProfile(user_id=c_user.id)
    db_session.add_all([musician, contractor])
    db_session.flush()

    past_date = now_peru().date() - timedelta(days=2)
    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=past_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Av. Principal 100",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("2.00"),
        platform_fee_amount=Decimal("10.00"),
        status=BookingStatus.in_progress,
    )
    db_session.add(booking)
    db_session.flush()
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=Decimal("500.00"),
            payment_type="full",
            status=PaymentStatus.retained,
        )
    )
    db_session.commit()

    # Contratista deja su reseña y músico deja su recomendación -> auto-transición a completed
    _login(client, c_user)
    r1 = client.post(
        f"/api/v1/bookings/{booking.id}/review",
        json={"rating": 5, "comment": "Excelente presentación musical"},
    )
    assert r1.status_code in (200, 201), r1.text

    _login(client, m_user)
    r2 = client.post(
        f"/api/v1/bookings/{booking.id}/recommend-contractor",
        json={"rating": 5, "comment": "Cliente muy puntual y amable"},
    )
    assert r2.status_code in (200, 201), r2.text

    db_session.refresh(booking)
    assert booking.status == BookingStatus.completed

    # Músico solicita retiro -> 200 OK y crea notificación para admin
    payout_res = client.post("/api/v1/payments/musician/request-payout")
    assert payout_res.status_code == 200, payout_res.text
    admin_notifs = (
        db_session.query(Notification)
        .filter(Notification.user_id == admin.id, Notification.type == "payout_requested")
        .all()
    )
    assert len(admin_notifs) == 1

    # Admin puede liquidar la reserva ahora que está en completed
    _login(client, admin)
    rel_res = client.post(
        f"/api/v1/admin/settlements/{booking.id}/release",
        json={"payout_reference": "BCP-LIQ-01"},
    )
    assert rel_res.status_code == 200, rel_res.text

    # Pago tardío en reserva cancelada notifica a admin una sola vez (SEC2-03 + SEC2-14)
    cancelled_b = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=5),
        start_time=time(19, 0),
        location_address="Av. Cancelada 1",
        event_type="Boda",
        price_agreed=Decimal("600.00"),
        platform_fee_percent=Decimal("2.00"),
        status=BookingStatus.cancelled,
    )
    db_session.add(cancelled_b)
    db_session.commit()

    payload = {
        "id": "mp-late-alert-1",
        "status": "approved",
        "external_reference": str(cancelled_b.id),
        "transaction_amount": float(contractor_payable_total(cancelled_b)),
        "currency_id": "PEN",
    }
    process_approved_mercadopago_payment(db_session, payload)
    process_approved_mercadopago_payment(db_session, payload)  # reintento idempotente

    alerts = (
        db_session.query(Notification)
        .filter(Notification.user_id == admin.id, Notification.type == "payment_alert")
        .all()
    )
    assert len(alerts) == 1


def test_sec2_04_sec2_08_booking_invite_preview_and_respond_with_hashed_token(client, db_session):
    """SEC2-04 y SEC2-08: El token de convocatoria se guarda con hash SHA-256 y preview/respond funcionan sin 500."""
    from datetime import timedelta
    from decimal import Decimal
    from app.core.timezone import now_peru
    from app.models.ensemble_member import BookingMemberInvite, EnsembleMemberStatus

    leader = User(email="lider.sec204@example.com", fullname="Líder", role=UserRole.musician, is_verified=True)
    m_user = User(email="integrante.sec204@example.com", fullname="Integrante", role=UserRole.musician)
    c_user = User(email="cliente.sec204@example.com", fullname="Cliente", role=UserRole.contractor)
    db_session.add_all([leader, m_user, c_user])
    db_session.flush()

    musician = MusicianProfile(user_id=leader.id, stage_name="Mariachi Líder 204", status=ProfileStatus.published)
    contractor = ContractorProfile(user_id=c_user.id)
    member = EnsembleMember(
        leader_user_id=leader.id,
        member_user_id=m_user.id,
        email=m_user.email,
        fullname=m_user.fullname,
        status=EnsembleMemberStatus.active,
    )
    db_session.add_all([musician, contractor, member])
    db_session.flush()

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=7),
        start_time=time(20, 0),
        location_address="Av. Evento 204",
        event_type="Boda",
        price_agreed=Decimal("700.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.commit()

    _login(client, leader)
    captured_urls = []
    with patch(
        "app.api.v1.endpoints.ensemble_members.send_booking_member_invite_email",
        side_effect=lambda *a, **kw: captured_urls.append(kw["respond_url"]),
    ):
        inv_res = client.post(
            f"/api/v1/bookings/{booking.id}/member-invites",
            json={"ensemble_member_ids": [str(member.id)]},
        )
    assert inv_res.status_code in (200, 201), inv_res.text
    assert len(captured_urls) == 1
    raw_token = captured_urls[0].rstrip("/").split("/")[-1]

    invite_row = db_session.query(BookingMemberInvite).filter(BookingMemberInvite.booking_id == booking.id).first()
    assert invite_row is not None
    assert invite_row.response_token != raw_token
    assert len(invite_row.response_token) == 64

    # Preview y respuesta con el token real funcionan y devuelven 200
    prev_res = client.get(f"/api/v1/invites/member/{raw_token}")
    assert prev_res.status_code == 200, prev_res.text
    resp_res = client.post(
        f"/api/v1/invites/member/{raw_token}/respond",
        json={"action": "accept"},
    )
    assert resp_res.status_code == 200, resp_res.text
    assert resp_res.json()["status"] == "accepted"


def test_sec2_09_unpaid_booking_does_not_block_public_search_availability(client, db_session):
    """SEC2-09: Una solicitud no pagada (requested) no bloquea al músico en el buscador público."""
    from datetime import timedelta
    from decimal import Decimal
    from app.core.timezone import now_peru

    m_user = User(email="m.sec209@example.com", fullname="Músico", role=UserRole.musician, is_verified=True)
    c_user = User(email="c.sec209@example.com", fullname="Cliente", role=UserRole.contractor)
    db_session.add_all([m_user, c_user])
    db_session.flush()

    musician = MusicianProfile(
        user_id=m_user.id,
        stage_name="Mariachi Disponible 209",
        slug="mariachi-disponible-209",
        status=ProfileStatus.published,
    )
    contractor = ContractorProfile(user_id=c_user.id)
    db_session.add_all([musician, contractor])
    db_session.flush()

    target_date = now_peru().date() + timedelta(days=12)
    unpaid = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=target_date,
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Calle 1",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.requested,
    )
    db_session.add(unpaid)
    db_session.commit()

    res = client.get(f"/api/v1/profiles/musicians?available_on={target_date.isoformat()}&search=Disponible 209")
    assert res.status_code == 200
    ids = [m["id"] for m in res.json()]
    assert str(musician.id) in ids


def test_sec2_10_sec2_11_sec2_12_verify_email_revokes_sessions_and_otp_cooldown(client, db_session):
    """SEC2-10, SEC2-11, SEC2-12:
    1) verify-email incrementa token_version e invalida cookies previas a la verificación.
    2) send-otp responde 200 uniforme cuando el correo no existe.
    3) Tras 5 intentos fallidos de OTP, pedir otro OTP dentro de los 10 min de bloqueo devuelve 429."""
    import hashlib
    from datetime import timedelta
    from decimal import Decimal
    from app.core.timezone import now_peru
    from app.models.booking_link import BookingLink, BookingLinkStatus

    raw_verify_tok = "verify-token-sec210"
    user = User(
        email="victim.sec210@example.com",
        fullname="Victim",
        role=UserRole.contractor,
        is_active=True,
        token_version=0,
        email_verification_token=hashlib.sha256(raw_verify_tok.encode("utf-8")).hexdigest(),
        email_verification_expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    db_session.add(user)
    db_session.commit()

    pre_cookie = create_access_token(str(user.id), user.role.value, version=0)
    v_res = client.post(f"/api/v1/auth/verify-email/{raw_verify_tok}")
    assert v_res.status_code == 200

    # La cookie emitida antes de verificar el correo queda revocada
    client.cookies.set("access_token", pre_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.clear()

    # SEC2-11: send-otp no filtra si el correo existe o no
    _, musician = _create_musician_for_link(db_session)
    link = BookingLink(
        musician_id=musician.id,
        token="link-sec211",
        price=Decimal("500.00"),
        event_date=now_peru().date() + timedelta(days=8),
        start_time=time(20, 0),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(link)
    db_session.commit()

    res_nonexistent = client.post(
        "/api/v1/booking-links/link-sec211/send-otp",
        json={"email": "noexiste.sec211@example.com"},
    )
    assert res_nonexistent.status_code == 200

    # SEC2-12: bloqueo de OTP no se resetea pidiendo un nuevo OTP durante la ventana de expiración
    with patch("app.services.email.service.send_templated_email"):
        client.post("/api/v1/booking-links/link-sec211/send-otp", json={"email": user.email})
    for _ in range(5):
        client.post("/api/v1/auth/login-otp", json={"email": user.email, "otp": "000000"})

    regen_res = client.post("/api/v1/booking-links/link-sec211/send-otp", json={"email": user.email})
    assert regen_res.status_code == 429


def _create_musician_for_link(db):
    u = User(
        email="m.link.sec211@example.com",
        fullname="Músico Link",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(u)
    db.flush()
    p = MusicianProfile(
        user_id=u.id,
        stage_name="Mariachi Link 211",
        slug="mariachi-link-211",
        status=ProfileStatus.published,
        signature_image_url="/uploads/private/firma-link-211.png",
    )
    db.add(p)
    db.flush()
    return u, p


def test_sec2_13_is_production_recognizes_prod_and_blocks_weak_config():
    """SEC2-13: ENVIRONMENT='prod' activa validaciones de producción en Settings."""
    from app.core.config import Settings

    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(
            ENVIRONMENT="prod",
            SQLALCHEMY_DATABASE_URI="postgresql://user:pass@prod-db:5432/chivapp",
            JWT_SECRET_KEY="short",
        )

