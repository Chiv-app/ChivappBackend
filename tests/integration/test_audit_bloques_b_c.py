from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.musician_profile import MusicianProfile, ProfileStatus
from app.models.payment import Payment, PaymentStatus
from app.models.payout_request import PayoutRequest
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _setup_users(db):
    admin = User(
        email="admin_bc@test.com",
        fullname="Admin BC",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    musician_user = User(
        email="musician_bc@test.com",
        fullname="Mariachi BC",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    contractor_user = User(
        email="contractor_bc@test.com",
        fullname="Contratista BC",
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
        stage_name="Mariachi BC",
        slug=f"mariachi-bc-{musician_user.id.hex[:6]}",
        status=ProfileStatus.published,
        signature_image_url="https://example.com/sig-bc.png",
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        city="Ayacucho",
    )
    db.add_all([musician, contractor])
    db.commit()
    return admin, musician_user, musician, contractor_user, contractor


def test_bug_14_musician_earnings_is_read_only(client, db_session):
    """BUG-14: GET /payments/musician/earnings must not mutate Booking status on read."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() - timedelta(days=1),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Av. Sol 123",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.in_progress,
    )
    db_session.add(booking)
    db_session.flush()

    db_session.add_all(
        [
            ContractorRecommendation(
                booking_id=booking.id,
                musician_id=musician.id,
                contractor_id=contractor.id,
                rating=5,
                comment="Excelente cliente",
            ),
            BookingReview(
                booking_id=booking.id,
                author_user_id=contractor_user.id,
                rating=5,
                comment="Gran show en vivo",
                is_final=True,
            ),
        ]
    )
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(musician_user.id), role="musician")
    )
    res = client.get("/api/v1/payments/musician/earnings")
    assert res.status_code == 200, res.text

    db_session.refresh(booking)
    assert booking.status == BookingStatus.in_progress


def test_fe_04_and_bug_15_admin_payout_requests_and_partial_settle(client, db_session):
    """FE-04 / BUG-15: Admin can list/process payout requests, and settling 1 of 2 bookings keeps request pending."""
    admin, musician_user, musician, contractor_user, contractor = _setup_users(db_session)

    b1 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() - timedelta(days=3),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Lima 1",
        event_type="Boda",
        price_agreed=Decimal("400.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
        status=BookingStatus.completed,
    )
    b2 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() - timedelta(days=2),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Lima 2",
        event_type="Cumpleaños",
        price_agreed=Decimal("600.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
        status=BookingStatus.completed,
    )
    db_session.add_all([b1, b2])
    db_session.flush()

    p1 = Payment(
        booking_id=b1.id,
        amount=Decimal("400.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
    )
    p2 = Payment(
        booking_id=b2.id,
        amount=Decimal("600.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
    )
    db_session.add_all(
        [
            BookingReview(
                booking_id=b1.id,
                author_user_id=contractor_user.id,
                rating=5,
                comment="Show 1 excelente",
                is_final=True,
            ),
            ContractorRecommendation(
                booking_id=b1.id,
                musician_id=musician.id,
                contractor_id=contractor.id,
                rating=5,
                comment="Cliente 1 puntual",
            ),
            BookingReview(
                booking_id=b2.id,
                author_user_id=contractor_user.id,
                rating=5,
                comment="Show 2 excelente",
                is_final=True,
            ),
            ContractorRecommendation(
                booking_id=b2.id,
                musician_id=musician.id,
                contractor_id=contractor.id,
                rating=5,
                comment="Cliente 2 puntual",
            ),
        ]
    )
    req = PayoutRequest(
        musician_id=musician.id,
        amount=Decimal("1000.00"),
        status="pending",
    )
    db_session.add_all([p1, p2, req])
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin"))

    # 1. Admin lists payout requests
    list_res = client.get("/api/v1/admin/payout-requests")
    assert list_res.status_code == 200, list_res.text
    items = list_res.json()
    assert any(item["id"] == str(req.id) for item in items)

    # 2. Admin settles ONLY b1 -> req must remain pending (BUG-15)
    settle_1 = client.post(
        f"/api/v1/admin/settlements/{b1.id}/release",
        json={"payout_reference": "OP-001"},
    )
    assert settle_1.status_code == 200, settle_1.text
    db_session.refresh(req)
    assert req.status == "pending"

    # 3. Admin settles b2 -> req now has all its bookings settled, so it transitions to processed
    settle_2 = client.post(
        f"/api/v1/admin/settlements/{b2.id}/release",
        json={"payout_reference": "OP-002"},
    )
    assert settle_2.status_code == 200, settle_2.text
    db_session.refresh(req)
    assert req.status == "processed"


def test_bug_10_booking_overlap_rejected(client, db_session):
    """BUG-10: Creating an overlapping booking for the same musician on the same date is rejected."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    future_date = now_peru().date() + timedelta(days=7)

    existing = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Av. Sol 100",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(existing)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res = client.post(
        "/api/v1/bookings",
        json={
            "musician_id": str(musician.id),
            "event_date": str(future_date),
            "start_time": "20:00:00",
            "end_time": "22:00:00",
            "duration_hours": 2,
            "location_address": "Av. Luna 200",
            "location_city": "Ayacucho",
            "event_type": "Aniversario",
        },
    )
    assert res.status_code == 400, res.text
    assert "reserva activa" in res.json()["detail"].lower()


def test_bug_12_quote_booking_refreshes_unsigned_contract(client, db_session):
    """BUG-12: Re-quoting a booking invalidates/updates any unsigned contract snapshot."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    future_date = now_peru().date() + timedelta(days=10)

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Cusco 123",
        location_city="Ayacucho",
        event_type="Serenata",
        price_agreed=Decimal("800.00"),
        status=BookingStatus.requested,
    )
    db_session.add(booking)
    db_session.flush()

    stale_contract = Contract(
        booking_id=booking.id,
        title="Contrato antiguo",
        body="<p>Contrato antiguo por S/ 800.00</p>",
        context={"{{monto_total}}": "S/ 800.00"},
    )
    db_session.add(stale_contract)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(musician_user.id), role="musician")
    )
    quote_res = client.post(
        f"/api/v1/bookings/{booking.id}/quote",
        json={"price_agreed": "1250.00", "musician_quote_notes": "Nuevo precio acordado"},
    )
    assert quote_res.status_code == 200, quote_res.text

    contract_res = client.get(f"/api/v1/contracts/booking/{booking.id}")
    assert contract_res.status_code == 200, contract_res.text
    contract_data = contract_res.json()
    assert contract_data["context"]["{{monto_total}}"] == "S/ 1,250.00"


def test_bug_11_sec_13_cannot_start_review_or_complete_before_event_date(client, db_session):
    """BUG-11 / SEC-13: Cannot start event phase, review, or complete a booking days before event_date."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    future_date = now_peru().date() + timedelta(days=5)

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(20, 0),
        end_time=time(22, 0),
        location_address="Av. Futuro 999",
        event_type="Boda",
        price_agreed=Decimal("1000.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(musician_user.id), role="musician")
    )
    start_res = client.post(f"/api/v1/bookings/{booking.id}/start-event")
    assert start_res.status_code == 400, start_res.text

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    review_res = client.post(
        f"/api/v1/bookings/{booking.id}/review",
        json={"rating": 5, "comment": "Intento prematuro"},
    )
    assert review_res.status_code == 400, review_res.text

    final_res = client.post(
        f"/api/v1/bookings/{booking.id}/final-review",
        json={"rating": 5, "comment": "Cierre prematuro"},
    )
    assert final_res.status_code == 400, final_res.text


def test_bug_25_check_username_endpoint_works(client, db_session):
    """BUG-25: GET /profiles/musician/check-username must not raise ImportError."""
    _, musician_user, _, _, _ = _setup_users(db_session)
    client.cookies.set(
        "access_token", create_access_token(subject=str(musician_user.id), role="musician")
    )
    res = client.get("/api/v1/profiles/check-username?username=mariachi_unico_2026")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["available"] is True


def test_sec_09_bug_34_public_share_masks_pii_and_validates_media_urls(client, db_session):
    """SEC-09 / BUG-34: Public share reviews mask author_user_id/email and reject external media URLs."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    # Give contractor an email as fullname to verify email masking in review_author_label
    contractor_user.fullname = "contratista.secreto@example.com"
    db_session.add(contractor_user)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date(),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Plaza Mayor",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.in_progress,
        share_token="public-share-tok-123",
        share_enabled=True,
    )
    db_session.add(booking)
    db_session.flush()

    review = BookingReview(
        booking_id=booking.id,
        author_user_id=contractor_user.id,
        rating=5,
        comment="Gran presentación",
    )
    db_session.add(review)
    db_session.commit()

    list_res = client.get("/api/v1/public/share/public-share-tok-123/reviews")
    assert list_res.status_code == 200, list_res.text
    reviews = list_res.json()
    assert len(reviews) == 1
    assert reviews[0]["author_user_id"] is None
    assert "contratista.secreto@example.com" not in (reviews[0].get("author_label") or "")

    # External photo_urls must be rejected
    bad_post = client.post(
        "/api/v1/public/share/public-share-tok-123/reviews",
        json={
            "rating": 5,
            "comment": "Intentando inyectar URL externa",
            "guest_name": "Invitado",
            "photo_urls": ["https://evil.example.com/tracker.jpg"],
        },
    )
    assert bad_post.status_code in (400, 422), bad_post.text
