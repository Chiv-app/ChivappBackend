from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserRole
from app.services.cancellation_policy import (
    execute_cancellation_refund,
    quote_cancellation,
    record_cancellation,
)
from app.services.platform_payment import contractor_payable_total

pytestmark = pytest.mark.db

PRICE = Decimal("1000")
FEE_PERCENT = Decimal("2")


def _booking(db, *, days_before: int, status=BookingStatus.payment_retained, pay=True):
    musician_user = User(email="m@test.com", fullname="Músico", role=UserRole.musician)
    contractor_user = User(email="c@test.com", fullname="Cliente", role=UserRole.contractor)
    db.add_all([musician_user, contractor_user])
    db.flush()
    musician = MusicianProfile(user_id=musician_user.id, stage_name="Mariachi Test")
    contractor = ContractorProfile(user_id=contractor_user.id)
    db.add_all([musician, contractor])
    db.flush()
    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() + timedelta(days=days_before),
        start_time=time(20, 0),
        location_address="Av. Test 123",
        event_type="Boda",
        status=status,
        price_agreed=PRICE,
        platform_fee_percent=FEE_PERCENT,
    )
    db.add(booking)
    db.flush()
    if pay:
        db.add(
            Payment(
                booking_id=booking.id,
                amount=contractor_payable_total(booking),
                payment_type="full",
                status=PaymentStatus.retained,
                gateway_provider="mercadopago",
                gateway_payment_id="mp-123",
            )
        )
    db.commit()
    return booking


APP_TOTAL = PRICE * (1 + FEE_PERCENT / 100)  # lo pagado sin el costo de pasarela


@pytest.mark.parametrize(
    ("days", "percent"),
    [(30, 100), (16, 100), (15, 50), (7, 50), (6, 0), (0, 0)],
)
def test_contractor_refund_tiers(db_session, days, percent):
    booking = _booking(db_session, days_before=days)
    quote = quote_cancellation(db_session, booking, "contractor")
    assert quote.refund_percent == percent
    assert quote.refund_amount == (APP_TOTAL * percent / 100).quantize(Decimal("0.01"))


def test_musician_cancellation_refunds_everything_paid(db_session):
    booking = _booking(db_session, days_before=2)
    quote = quote_cancellation(db_session, booking, "musician")
    assert quote.refund_percent == 100
    assert quote.refund_amount == contractor_payable_total(booking)


def test_no_payment_no_refund(db_session):
    booking = _booking(db_session, days_before=30, status=BookingStatus.accepted, pay=False)
    quote = quote_cancellation(db_session, booking, "contractor")
    assert quote.refund_amount == 0


def test_partial_refund_splits_payment_and_keeps_rest_for_musician(db_session):
    booking = _booking(db_session, days_before=10)
    paid = contractor_payable_total(booking)
    quote = quote_cancellation(db_session, booking, "contractor")
    record_cancellation(booking, quote)
    db_session.commit()

    with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 1}) as refund:
        execute_cancellation_refund(db_session, booking)

    refund.assert_called_once()
    assert refund.call_args.args[0] == "mp-123"
    assert booking.cancellation_refund_status == "completed"

    retained = db_session.query(Payment).filter(Payment.status == PaymentStatus.retained).one()
    refund_row = db_session.query(Payment).filter(Payment.payment_type == "refund").one()
    assert refund_row.amount == quote.refund_amount
    assert retained.amount + refund_row.amount == paid


def test_failed_refund_is_retryable_without_double_refund(db_session):
    booking = _booking(db_session, days_before=30)
    record_cancellation(booking, quote_cancellation(db_session, booking, "contractor"))
    db_session.commit()

    with patch("app.services.mercadopago_service.issue_refund", side_effect=ValueError("MP caído")):
        execute_cancellation_refund(db_session, booking)
    assert booking.cancellation_refund_status == "failed"
    assert db_session.query(Payment).filter(Payment.payment_type == "refund").count() == 0

    with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 2}) as refund:
        execute_cancellation_refund(db_session, booking)
        execute_cancellation_refund(db_session, booking)  # segundo intento: nada pendiente
    assert refund.call_count == 1
    assert booking.cancellation_refund_status == "completed"


def test_cancel_endpoint_applies_policy_and_refunds(client, db_session):
    res = client.post(
        "/api/v1/auth/guest-register",
        json={"email": "cliente@example.com", "fullname": "Cliente", "phone": "999555666"},
    )
    assert res.status_code == 201
    contractor = db_session.query(ContractorProfile).one()

    booking = _booking(db_session, days_before=10)
    booking.contractor_id = contractor.id
    db_session.commit()

    quote = client.get(f"/api/v1/bookings/{booking.id}/cancellation-quote").json()
    assert quote["can_cancel"] is True
    assert quote["refund_percent"] == 50

    with patch("app.services.mercadopago_service.issue_refund", return_value={"id": 9}) as refund:
        res = client.post(f"/api/v1/bookings/{booking.id}/cancel", json={"rejection_reason": "Cambio de planes"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_by"] == "contractor"
    assert body["cancellation_refund_status"] == "completed"
    assert Decimal(str(body["cancellation_refund_amount"])) == Decimal(str(quote["refund_amount"]))
    refund.assert_called_once()
