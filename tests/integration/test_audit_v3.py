"""Tests de integración para la ronda 3 de auditoría (v3)."""

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.jwt import create_access_token, create_oauth_pending_token
from app.core.hashing import hash_password
from app.core.timezone import now_peru
from app.services.email.auth_emails import issue_email_verification_token
from app.models.booking import Booking, BookingStatus
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile, ProfileStatus
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.payout_request import PayoutRequest
from app.models.user import User, UserRole
from app.services.mercadopago_service import process_approved_mercadopago_payment
from app.services.platform_payment import contractor_payable_total

pytestmark = pytest.mark.db


def _setup_users(db):
    admin = User(
        email="admin_v3@test.com",
        fullname="Admin V3",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    musician_user = User(
        email="musician_v3@test.com",
        fullname="Mariachi V3",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    contractor_user = User(
        email="contractor_v3@test.com",
        fullname="Contratista V3",
        phone="999888777",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add_all([admin, musician_user, contractor_user])
    db.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi V3",
        slug=f"mariachi-v3-{musician_user.id.hex[:6]}",
        status=ProfileStatus.published,
        signature_image_url="/uploads/private/firma-v3.png",
        contract_template_title="Contrato V3",
        contract_template_body=(
            "<p>Contrato de prestación de servicios musicales profesionales celebrado entre las partes "
            "por el monto total de {{monto_total}} para la fecha {{fecha_evento}} en {{lugar_evento}}.</p>"
        ),
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        status=ProfileStatus.published,
    )
    db.add_all([musician, contractor])
    db.commit()
    return admin, musician_user, musician, contractor_user, contractor


def _auth_cookie(client, user: User):
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(
            subject=str(user.id),
            role=user.role.value,
            version=user.token_version or 0,
        ),
    )


def test_bug3_01_fe3_02_bug3_13_booking_link_public_get_empty_dni_and_cancel_expiry(
    client, db_session
):
    """BUG3-01 (GET /booking-links/{token} 200), FE3-02 (empty document_number), BUG3-13 (cancel expires link)."""
    _, musician_user, musician, _, _ = _setup_users(db_session)
    event_date = now_peru().date() + timedelta(days=10)

    link = BookingLink(
        token="tok-v3-public-check",
        musician_id=musician.id,
        price=Decimal("600.00"),
        event_date=event_date,
        start_time=time(20, 0),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=3),
    )
    db_session.add(link)
    db_session.commit()

    # 1. GET /booking-links/{token} returns 200 and includes cancellation_policy
    get_res = client.get(f"/api/v1/booking-links/{link.token}")
    assert get_res.status_code == 200, get_res.text
    body = get_res.json()
    assert body["cancellation_policy"]["full_refund_min_days"] >= 0
    assert 0 <= body["cancellation_policy"]["partial_refund_percent"] <= 100

    # 2. Checkout with empty document_number: "" succeeds (FE3-02) and stores contract hash (SEC3-06)
    checkout_res = client.post(
        f"/api/v1/booking-links/{link.token}/checkout",
        json={
            "fullname": "Cliente Nuevo Link",
            "email": "nuevo_link_v3@test.com",
            "phone": "987654321",
            "document_number": "",
            "event_date": str(event_date),
            "start_time": "20:00:00",
            "location_address": "Jr. Lima 456",
            "location_city": "Ayacucho",
            "event_type": "Serenata",
        },
    )
    assert checkout_res.status_code == 200, checkout_res.text
    booking_id = checkout_res.json()["booking_id"]
    contract = db_session.query(Contract).filter(Contract.booking_id == booking_id).first()
    assert contract is not None
    assert (contract.context or {}).get("__signed_body_sha256__")

    # 3. Cancelling the linked booking expires the BookingLink (BUG3-13)
    _auth_cookie(client, musician_user)
    cancel_res = client.post(
        f"/api/v1/bookings/{booking_id}/cancel",
        json={"cancellation_reason": "Fuerza mayor"},
    )
    assert cancel_res.status_code == 200, cancel_res.text
    db_session.refresh(link)
    assert link.status == BookingLinkStatus.expired


def test_bug3_02_overlap_blocked_on_accept_quote_and_webhook(client, db_session):
    """BUG3-02: Overlap rejected on accept-quote and concurrent approved webhook marks needs_refund."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    event_date = now_peru().date() + timedelta(days=12)

    b1 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=event_date,
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Av. 1",
        event_type="Boda",
        price_agreed=Decimal("700.00"),
        status=BookingStatus.payment_retained,
    )
    b2 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=event_date,
        start_time=time(20, 0),
        end_time=time(22, 0),
        location_address="Av. 2",
        event_type="Cumpleaños",
        price_agreed=Decimal("700.00"),
        status=BookingStatus.accepted,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    # 1. accept-quote on b2 fails because b1 already occupies 19:00-21:00
    _auth_cookie(client, contractor_user)
    acc_res = client.post(
        f"/api/v1/bookings/{b2.id}/accept-quote",
        json={"accept_terms": True},
    )
    assert acc_res.status_code in (400, 409)

    # 2. If b2 was already contract_pending before b1 paid, an approved MP webhook for b2 marks needs_refund due to schedule_overlap
    b2.status = BookingStatus.contract_pending
    b2.platform_fee_percent = Decimal("0.00")
    b2.platform_fee_amount = Decimal("0.00")
    db_session.commit()
    expected_b2_total = float(contractor_payable_total(b2))
    payment = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "9988776655",
            "external_reference": str(b2.id),
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_b2_total,
            "currency_id": "PEN",
            "payment_method_id": "visa",
            "payment_type_id": "credit_card",
        },
    )
    db_session.refresh(b2)
    assert b2.status != BookingStatus.payment_retained
    assert payment is not None
    assert (payment.gateway_metadata or {}).get("needs_refund") is True
    assert (payment.gateway_metadata or {}).get("needs_refund_reason") == "schedule_overlap"
    assert payment.status == PaymentStatus.initiated


def test_bug3_03_and_bug3_05_overnight_event_and_no_premature_completion(
    client, db_session
):
    """BUG3-03/SEC3-02 & BUG3-05: Overnight events allowed; mid-show reaction + recommendation does not complete."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    today = now_peru().date()

    # 1. Create overnight booking (22:00 -> 02:00 next day) via API
    _auth_cookie(client, contractor_user)
    create_res = client.post(
        "/api/v1/bookings",
        json={
            "musician_id": str(musician.id),
            "event_date": str(today + timedelta(days=5)),
            "start_time": "22:00:00",
            "end_time": "02:00:00",
            "location_address": "Av. Nocturna 100",
            "event_type": "Fiesta",
        },
    )
    assert create_res.status_code == 201, create_res.text

    # 2. Booking currently in progress (started 30m ago, ends in 90m)
    now_local = now_peru()
    start_dt = now_local - timedelta(minutes=30)
    end_dt = now_local + timedelta(minutes=90)
    in_progress_booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=start_dt.date(),
        start_time=start_dt.time().replace(microsecond=0),
        end_time=end_dt.time().replace(microsecond=0),
        location_address="Jr. En Curso 123",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.in_progress,
    )
    db_session.add(in_progress_booking)
    db_session.commit()

    # Contractor posts a mid-show reaction (emoji, no final review)
    _auth_cookie(client, contractor_user)
    rev_res = client.post(
        f"/api/v1/bookings/{in_progress_booking.id}/review",
        json={"rating": 5, "emoji": "🔥", "is_final": False},
    )
    assert rev_res.status_code == 200, rev_res.text

    # Musician posts contractor recommendation while show is still in progress -> does NOT prematurely complete booking
    _auth_cookie(client, musician_user)
    rec_res = client.post(
        f"/api/v1/bookings/{in_progress_booking.id}/recommend-contractor",
        json={"rating": 5, "comment": "Excelente cliente durante el evento"},
    )
    assert rec_res.status_code == 200, rec_res.text
    db_session.refresh(in_progress_booking)
    assert in_progress_booking.status == BookingStatus.in_progress


def test_sec3_05_bug3_04_admin_payout_process_releases_retained_payments(
    client, db_session
):
    """SEC3-05 / BUG3-04: Processing a payout request releases retained payments and prevents double withdrawal."""
    admin, musician_user, musician, _, contractor = _setup_users(db_session)
    past_date = now_peru().date() - timedelta(days=5)

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=past_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Av. Sol 50",
        event_type="Serenata",
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("10.00"),
        platform_fee_amount=Decimal("100.00"),
        status=BookingStatus.completed,
    )
    db_session.add(booking)
    db_session.flush()

    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("1000.00"),
        status=PaymentStatus.retained,
        retained_at=datetime.utcnow() - timedelta(days=6),
    )
    payout_req = PayoutRequest(
        musician_id=musician.id,
        amount=Decimal("870.70"),
        status="pending",
        notes=f"Solicitud de prueba {booking.id}",
    )
    db_session.add_all([payment, payout_req])
    db_session.commit()

    _auth_cookie(client, admin)
    proc_res = client.post(
        f"/api/v1/admin/payout-requests/{payout_req.id}/process",
        json={"notes": "Transferido OP-999111"},
    )
    assert proc_res.status_code == 200, proc_res.text
    assert admin.email in (proc_res.json().get("notes") or "")

    db_session.refresh(payment)
    assert payment.status == PaymentStatus.released

    # Musician cannot request payout again for the same released balance
    _auth_cookie(client, musician_user)
    second_req = client.post(
        "/api/v1/payments/musician/request-payout",
        json={},
    )
    assert second_req.status_code == 400


def test_bug3_06_invalid_amount_webhook_flags_needs_refund_and_alerts_admin(
    client, db_session
):
    """BUG3-06: Approved payment with wrong amount sets needs_refund=True and notifies admins."""
    admin, _, musician, _, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=9),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Puno 10",
        event_type="Serenata",
        price_agreed=Decimal("800.00"),
        status=BookingStatus.contract_pending,
    )
    db_session.add(booking)
    db_session.commit()

    payment = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "1122334455",
            "external_reference": str(booking.id),
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 50.0,  # Wrong amount!
            "currency_id": "PEN",
        },
    )
    assert payment is not None
    assert (payment.gateway_metadata or {}).get("needs_refund") is True
    assert (payment.gateway_metadata or {}).get("needs_refund_reason") is not None
    admin_notifs = (
        db_session.query(Notification)
        .filter(
            Notification.user_id == admin.id,
            Notification.type == "payment_alert",
        )
        .all()
    )
    assert len(admin_notifs) >= 1

    _auth_cookie(client, admin)
    list_res = client.get("/api/v1/admin/payments")
    assert list_res.status_code == 200
    found = [p for p in list_res.json() if p["id"] == str(payment.id)]
    assert found and found[0]["needs_refund"] is True


def test_sec3_06_contract_snapshot_immutable_and_hash_checked(client, db_session):
    """SEC3-06: Quote snapshots contract; GET does not overwrite when musician edits template; hash mismatch -> 409."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=14),
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Av. Arequipa 200",
        event_type="Boda",
        status=BookingStatus.requested,
    )
    db_session.add(booking)
    db_session.commit()

    _auth_cookie(client, musician_user)
    q_res = client.post(
        f"/api/v1/bookings/{booking.id}/quote",
        json={"price_agreed": "950.00", "musician_quote_notes": "Incluye sonido"},
    )
    assert q_res.status_code == 200, q_res.text

    # Musician changes template after quoting
    musician.contract_template_body = "<p>PLANTILLA MODIFICADA DESPUÉS DE COTIZAR {{monto_total}}</p>"
    db_session.commit()

    _auth_cookie(client, contractor_user)
    c_res = client.get(f"/api/v1/contracts/booking/{booking.id}")
    assert c_res.status_code == 200, c_res.text
    c_data = c_res.json()
    assert "PLANTILLA MODIFICADA DESPUÉS DE COTIZAR" not in c_data["body"]
    assert c_data["body_sha256"]

    # Submitting a stale/wrong contract_body_sha256 is rejected with 409
    bad_accept = client.post(
        f"/api/v1/bookings/{booking.id}/accept-quote",
        json={"accept_terms": True, "contract_body_sha256": "0" * 64},
    )
    assert bad_accept.status_code == 409

    # Submitting the matching contract_body_sha256 succeeds and records signed_body_sha256
    ok_accept = client.post(
        f"/api/v1/bookings/{booking.id}/accept-quote",
        json={"accept_terms": True, "contract_body_sha256": c_data["body_sha256"]},
    )
    assert ok_accept.status_code == 200, ok_accept.text
    c_after = client.get(f"/api/v1/contracts/booking/{booking.id}").json()
    assert c_after["signed_body_sha256"] == c_data["body_sha256"]


def test_sec3_03_and_sec3_04_pre_register_verify_and_otp_attempt_persistence(
    client, db_session
):
    """SEC3-03 (verify-email invalidates unverified pre-registered password) & SEC3-04 (OTP attempts not reset)."""
    # 1. SEC3-03: Attacker registers victim's email with known password; victim clicks verify-email in own browser
    reg_res = client.post(
        "/api/v1/auth/register",
        json={
            "email": "victima_v3@test.com",
            "password": "AttackerPassword1!",
            "fullname": "Víctima",
            "phone": "999111222",
            "role": "contractor",
            "accepted_terms": True,
        },
    )
    assert reg_res.status_code == 201, reg_res.text
    victim = db_session.query(User).filter(User.email == "victima_v3@test.com").first()
    assert victim is not None and victim.email_verification_token
    raw_token = issue_email_verification_token(victim)
    db_session.commit()

    # Before email verification, login with password is blocked (SEC4-01)
    client.cookies.clear()
    pre_login = client.post(
        "/api/v1/auth/login",
        json={"email": "victima_v3@test.com", "password": "AttackerPassword1!"},
    )
    assert pre_login.status_code == 403

    # Victim verifies without attacker's session cookie: token_version rotates and pre-registered password_hash is cleared (SEC3-03 / SEC5-04)
    db_session.refresh(victim)
    old_version = victim.token_version or 0
    ver_res = client.post(f"/api/v1/auth/verify-email/{raw_token}")
    assert ver_res.status_code == 200, ver_res.text
    db_session.refresh(victim)
    assert victim.password_hash is None
    assert (victim.token_version or 0) >= old_version + 1
    post_login = client.post(
        "/api/v1/auth/login",
        json={"email": "victima_v3@test.com", "password": "AttackerPassword1!"},
    )
    assert post_login.status_code == 401

    # 2. SEC3-04: OTP attempts persist when login-step-one is called again during active OTP window
    otp_user = User(
        email="otp_v3@test.com",
        fullname="OTP User",
        role=UserRole.contractor,
        password_hash=hash_password("ValidPass123!"),
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
        login_otp_hash="dummy-hash",
        login_otp_expires_at=datetime.utcnow() + timedelta(minutes=5),
        login_otp_attempts=4,
    )
    db_session.add(otp_user)
    db_session.commit()

    # Force cooldown to allow re-sending OTP while window is still active
    step1 = client.post(
        "/api/v1/auth/login-step-one",
        json={"email": "otp_v3@test.com"},
    )
    assert step1.status_code == 200
    db_session.refresh(otp_user)
    assert otp_user.login_otp_attempts == 4  # Not reset to 0!


def test_bug3_07_bug3_08_and_fe3_06_oauth_homonyms_guest_reuse_and_unread_header(
    client, db_session
):
    """BUG3-07 (OAuth homonyms), BUG3-08 (musician-create without email reuses guest), FE3-06 (X-Unread-Count)."""
    _, musician_user, _, _, _ = _setup_users(db_session)

    # 1. BUG3-07: Two musicians with the same Google fullname can complete OAuth
    for idx in (1, 2):
        pending_token = create_oauth_pending_token(
            {
                "provider": "google",
                "provider_user_id": f"google-homonym-{idx}",
                "email": f"homonimo_oauth_{idx}@test.com",
                "fullname": "Mariachi Sol Real",
                "picture_url": None,
                "email_verified": True,
            }
        )
        client.cookies.set("oauth_pending", pending_token)
        res = client.post(
            "/api/v1/auth/oauth/complete",
            json={
                "role": "musician",
                "phone": f"91122233{idx}",
                "accepted_terms": True,
            },
        )
        assert res.status_code == 200, res.text

    # 2. BUG3-08: Musician creates two manual bookings for the same walk-in client without email
    _auth_cookie(client, musician_user)
    for day_offset in (20, 22):
        mc_res = client.post(
            "/api/v1/bookings/musician-created",
            json={
                "contractor_fullname": "Cliente Sin Correo",
                "contractor_email": "",
                "contractor_phone": "944555666",
                "event_date": str(now_peru().date() + timedelta(days=day_offset)),
                "start_time": "18:00:00",
                "end_time": "20:00:00",
                "location_address": "Jr. Walkin 10",
                "event_type": "Serenata",
                "price_agreed": "500.00",
            },
        )
        assert mc_res.status_code == 201, mc_res.text

    # 3. FE3-06: GET /notifications/me exposes X-Unread-Count header
    notif_res = client.get("/api/v1/notifications/me?limit=5")
    assert notif_res.status_code == 200
    assert "x-unread-count" in {k.lower() for k in notif_res.headers.keys()}
