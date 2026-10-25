from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.api import deps
from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.main import app
from app.models.booking import Booking, BookingStatus
from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile, ProfileStatus
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserRole
from app.services.cancellation_policy import quote_cancellation, record_cancellation
from app.services.mercadopago_service import process_approved_mercadopago_payment
from app.services.platform_payment import contractor_payable_total
from tests.conftest import TestingSessionLocal

pytestmark = pytest.mark.db


def _create_verified_published_musician(db, email="musician@test.com"):
    user = User(
        email=email,
        fullname="Mariachi Sol",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name="Mariachi Sol",
        slug=f"mariachi-sol-{user.id.hex[:6]}",
        status=ProfileStatus.published,
        signature_image_url="/uploads/private/firma-sol.png",
    )
    db.add(profile)
    db.commit()
    return user, profile


def test_booking_links_me_does_not_500(client, db_session):
    """BUG-06 / FE-02: GET /booking-links/me must return 200 and list links without AttributeError."""
    user, profile = _create_verified_published_musician(db_session)
    link = BookingLink(
        musician_id=profile.id,
        token="test-token-123",
        price=Decimal("1000.00"),
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(20, 0),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(link)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(user.id), role="musician"))
    res = client.get("/api/v1/booking-links/me")
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data) == 1
    assert data[0]["token"] == "test-token-123"


def test_booking_link_requires_verified_published_musician(client, db_session):
    """SEC-03: Unverified or draft musicians cannot create or checkout booking links."""
    user = User(
        email="draft@test.com",
        fullname="Draft Musician",
        role=UserRole.musician,
        is_active=True,
        is_verified=False,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(user)
    db_session.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name="Draft Group",
        slug="draft-group",
        status=ProfileStatus.draft,
    )
    db_session.add(profile)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(user.id), role="musician"))
    res = client.post(
        "/api/v1/booking-links",
        json={
            "price": 500.0,
            "event_date": str(now_peru().date() + timedelta(days=5)),
            "start_time": "19:00:00",
        },
    )
    assert res.status_code == 403


def test_send_otp_sends_email_and_locks_out_after_5_attempts_and_blocks_admin(client, db_session):
    """BUG-04 / SEC-02: send-otp uses proper send_templated_email signature, blocks admin, and enforces 5-attempt lockout."""
    _, profile = _create_verified_published_musician(db_session)
    link = BookingLink(
        musician_id=profile.id,
        token="otp-token-123",
        price=Decimal("1000.00"),
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(20, 0),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    contractor = User(
        email="recurring@test.com",
        fullname="Cliente Recurrente",
        role=UserRole.contractor,
        is_active=True,
    )
    admin = User(
        email="boss@test.com",
        fullname="Admin User",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add_all([link, contractor, admin])
    db_session.commit()

    # Admin cannot request OTP
    res_admin = client.post("/api/v1/auth/login-step-one", json={"email": "boss@test.com"})
    assert res_admin.status_code == 200
    assert res_admin.json()["status"] == "requires_password"
    db_session.refresh(admin)
    assert admin.login_otp_hash is None

    # Recurring client receives OTP via booking-links send-otp
    with patch("app.services.email.service.send_templated_email") as mock_email:
        res_otp = client.post(
            "/api/v1/booking-links/otp-token-123/send-otp",
            json={"email": "recurring@test.com"},
        )
        assert res_otp.status_code == 200, res_otp.text
        mock_email.assert_called_once()
        assert mock_email.call_args.kwargs["slug"] == "login_otp"
        assert mock_email.call_args.kwargs["to"] == "recurring@test.com"
        otp_sent = mock_email.call_args.kwargs["context"]["otp_code"]
        assert len(otp_sent) == 6

    # 5 wrong attempts trigger lockout
    for i in range(5):
        res_bad = client.post(
            "/api/v1/auth/login-otp",
            json={"email": "recurring@test.com", "otp": "000000"},
        )
        if i < 4:
            assert res_bad.status_code == 401
        else:
            assert res_bad.status_code == 429

    # Even the valid OTP is invalidated after lockout
    res_after = client.post(
        "/api/v1/auth/login-otp",
        json={"email": "recurring@test.com", "otp": otp_sent},
    )
    assert res_after.status_code in (401, 429)


def test_checkout_blocks_account_takeover_without_credentials(client, db_session):
    """SEC-01 / BUG-05: Existing user with email_verification_token cannot be hijacked in checkout without OTP/password."""
    _, profile = _create_verified_published_musician(db_session)
    link = BookingLink(
        musician_id=profile.id,
        token="sec01-token",
        price=Decimal("800.00"),
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(18, 0),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    victim = User(
        email="victim@test.com",
        fullname="Victim User",
        role=UserRole.contractor,
        is_active=True,
        email_verified_at=None,
        email_verification_token="stale-token-hash",
    )
    db_session.add_all([link, victim])
    db_session.flush()
    db_session.add(ContractorProfile(user_id=victim.id))
    db_session.commit()

    # Check-identity must require verification (exists_verified), never exists_unverified
    res_id = client.post(
        "/api/v1/booking-links/sec01-token/check-identity",
        json={"email": "victim@test.com"},
    )
    assert res_id.status_code == 200
    assert res_id.json()["status"] == "exists_verified"

    # Attempting checkout without OTP or password must return 401 and set no cookie
    client.cookies.clear()
    res_checkout = client.post(
        "/api/v1/booking-links/sec01-token/checkout",
        json={
            "event_date": str(now_peru().date() + timedelta(days=10)),
            "start_time": "18:00:00",
            "event_type": "Boda",
            "location_address": "Calle Falsa 123",
            "location_city": "Lima",
            "email": "victim@test.com",
            "fullname": "Attacker Name",
            "phone": "999000000",
            "document_number": "12345678",
        },
    )
    assert res_checkout.status_code == 401
    assert "access_token" not in res_checkout.cookies


def test_mercadopago_payment_does_not_resurrect_cancelled_or_completed_booking(db_session):
    """SEC-04 / BUG-03 & BUG-07 / BUG-08: Approved MP payment must not resurrect cancelled/completed bookings, and handles missing end_time."""
    _, musician = _create_verified_published_musician(db_session)
    c_user = User(email="c@test.com", fullname="Cliente", role=UserRole.contractor)
    db_session.add(c_user)
    db_session.flush()
    contractor = ContractorProfile(user_id=c_user.id)
    db_session.add(contractor)
    db_session.flush()

    cancelled_booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=15),
        start_time=time(20, 0),
        end_time=None,
        location_address="Av. Test 123",
        event_type="Boda",
        status=BookingStatus.cancelled,
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("2.00"),
    )
    db_session.add(cancelled_booking)
    db_session.commit()

    expected_total = float(contractor_payable_total(cancelled_booking))
    payment_row = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "mp-late-999",
            "status": "approved",
            "external_reference": str(cancelled_booking.id),
            "transaction_amount": expected_total,
            "currency_id": "PEN",
        },
    )
    db_session.refresh(cancelled_booking)
    assert cancelled_booking.status == BookingStatus.cancelled
    assert payment_row is not None
    assert payment_row.booking_id == cancelled_booking.id
    assert payment_row.status == PaymentStatus.initiated

    # Now test in_process -> approved transition on a valid booking without end_time (BUG-07 / BUG-08)
    valid_booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=15),
        start_time=time(20, 0),
        end_time=None,
        location_address="Av. Test 456",
        event_type="Boda",
        status=BookingStatus.accepted,
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("2.00"),
    )
    db_session.add(valid_booking)
    db_session.flush()
    in_proc_payment = Payment(
        booking_id=valid_booking.id,
        amount=contractor_payable_total(valid_booking),
        payment_type="full",
        status=PaymentStatus.initiated,
        gateway_provider="mercadopago",
        gateway_payment_id="mp-inproc-1",
    )
    db_session.add(in_proc_payment)
    db_session.commit()

    valid_payment_row = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "mp-inproc-1",
            "status": "approved",
            "external_reference": str(valid_booking.id),
            "transaction_amount": float(contractor_payable_total(valid_booking)),
            "currency_id": "PEN",
        },
    )
    db_session.refresh(valid_booking)
    assert valid_payment_row is not None
    assert valid_payment_row.status == PaymentStatus.retained
    assert valid_booking.status == BookingStatus.payment_retained


def test_admin_endpoints_with_fresh_session_and_settle_refund_and_block_unresolved_release(client, db_session):
    """BUG-01, BUG-02, BUG-09, SEC-08: Admin cancel/approve/settle work with independent sessions and record refunds safely."""
    _, musician = _create_verified_published_musician(db_session, email="m2@test.com")
    c_user = User(email="c2@test.com", fullname="Cliente 2", role=UserRole.contractor)
    admin = User(email="admin2@test.com", fullname="Admin 2", role=UserRole.admin)
    db_session.add_all([c_user, admin])
    db_session.flush()
    contractor = ContractorProfile(user_id=c_user.id)
    db_session.add(contractor)
    db_session.flush()

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=20),
        start_time=time(20, 0),
        location_address="Av. Test 789",
        event_type="Boda",
        status=BookingStatus.payment_retained,
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("2.00"),
    )
    db_session.add(booking)
    db_session.flush()
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=contractor_payable_total(booking),
            payment_type="full",
            status=PaymentStatus.retained,
            gateway_provider="mercadopago",
            gateway_payment_id="mp-admin-1",
        )
    )
    db_session.commit()
    booking_id = booking.id
    admin_id = admin.id

    # Override get_db to yield a fresh session per request (reproducing real runtime behavior for BUG-01)
    def _per_request_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = _per_request_db
    try:
        client.cookies.set("access_token", create_access_token(subject=str(admin_id), role="admin"))

        # 1. Admin cancel booking on fresh session (BUG-01)
        with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 66}):
            res_cancel = client.post(
                f"/api/v1/admin/bookings/{booking_id}/cancel",
                json={"reason": "Prueba", "refund_percent": 50},
            )
            assert res_cancel.status_code == 200, res_cancel.text
            assert res_cancel.json()["cancellation_refund_status"] == "completed"

        # 2. Contractor-cancelled booking in pending_approval: test BUG-09 & BUG-01 approve-refund
        pending_booking = Booking(
            contractor_id=contractor.id,
            musician_id=musician.id,
            event_date=now_peru().date() + timedelta(days=20),
            start_time=time(20, 0),
            location_address="Av. Test 790",
            event_type="Boda",
            status=BookingStatus.payment_retained,
            price_agreed=Decimal("1000.00"),
            platform_fee_percent=Decimal("2.00"),
        )
        db_session.add(pending_booking)
        db_session.flush()
        db_session.add(
            Payment(
                booking_id=pending_booking.id,
                amount=contractor_payable_total(pending_booking),
                payment_type="full",
                status=PaymentStatus.retained,
                gateway_provider="mercadopago",
                gateway_payment_id="mp-admin-2",
            )
        )
        db_session.flush()
        record_cancellation(
            pending_booking,
            quote_cancellation(db_session, pending_booking, "contractor"),
            approved=False,
        )
        db_session.commit()
        pending_booking_id = pending_booking.id

        # BUG-09: Releasing musician payment while refund is pending_approval must fail with 400
        res_release_blocked = client.post(
            f"/api/v1/admin/settlements/{pending_booking_id}/release",
            json={},
        )
        assert res_release_blocked.status_code == 400

        # BUG-01: Approve refund on fresh session must succeed without InvalidRequestError
        with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 77}) as mock_refund:
            res_approve = client.post(
                f"/api/v1/admin/bookings/{pending_booking_id}/approve-refund",
                json={"amount": 200.0},
            )
            assert res_approve.status_code == 200, res_approve.text
            assert res_approve.json()["cancellation_refund_status"] == "completed"
            mock_refund.assert_called_once()

        # 3. BUG-02 / SEC-08: Create a completed booking with a responded complaint and settle with contractor_refund > 0
        dispute_booking = Booking(
            contractor_id=contractor.id,
            musician_id=musician.id,
            event_date=now_peru().date() - timedelta(days=1),
            start_time=time(20, 0),
            location_address="Av. Disputa 100",
            event_type="Boda",
            status=BookingStatus.completed,
            price_agreed=Decimal("1000.00"),
            platform_fee_percent=Decimal("2.00"),
        )
        db_session.add(dispute_booking)
        db_session.flush()
        db_session.add(
            Payment(
                booking_id=dispute_booking.id,
                amount=contractor_payable_total(dispute_booking),
                payment_type="full",
                status=PaymentStatus.retained,
                gateway_provider="mercadopago",
                gateway_payment_id="mp-dispute-1",
            )
        )
        complaint = BookingComplaint(
            booking_id=dispute_booking.id,
            opened_by_user_id=c_user.id,
            reason="Llegaron tarde",
            evidence_url="https://example.com/ev.jpg",
            status=BookingComplaintStatus.musician_responded,
        )
        db_session.add(complaint)
        db_session.commit()
        complaint_id = complaint.id
        dispute_booking_id = dispute_booking.id

        with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 88}) as mock_settle_ref:
            res_settle = client.post(
                f"/api/v1/admin/settlements/{dispute_booking_id}/settle",
                json={
                    "musician_amount": 700.0,
                    "contractor_refund": 300.0,
                    "notes": "Acuerdo parcial",
                },
            )
            assert res_settle.status_code == 200, res_settle.text
            mock_settle_ref.assert_called_once()
            assert mock_settle_ref.call_args.kwargs.get("idempotency_key") == f"complaint-refund-{complaint_id}"
    finally:
        app.dependency_overrides[deps.get_db] = lambda: db_session


def test_forgot_password_allows_guest_account_without_password_hash(client, db_session):
    """BG-02: Guest accounts without password_hash can request password reset and verify email upon reset."""
    guest = User(
        email="guest@test.com",
        fullname="Invitado",
        role=UserRole.contractor,
        is_active=True,
        email_verified_at=None,
        password_hash=None,
    )
    db_session.add(guest)
    db_session.commit()

    with patch("app.api.v1.endpoints.auth.send_password_reset_email") as mock_send:
        res = client.post("/api/v1/auth/forgot-password", json={"email": "guest@test.com"})
        assert res.status_code == 200
        mock_send.assert_called_once()
