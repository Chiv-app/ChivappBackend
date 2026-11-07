"""Reglas de liquidación: contracargo automático, queja con reembolso y queja sin respuesta del músico."""

from datetime import time, timedelta
from decimal import Decimal

import pytest

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.booking_complaint import (
    REFUND_STATUS_COMPLETED,
    BookingComplaint,
    BookingComplaintStatus,
)
from app.models.contractor_profile import ContractorProfile
from app.models.ledger_entry import LedgerEntry
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserRole
from app.services.platform_payment import contractor_payable_total
from app.services.settlement import (
    musician_net_payable,
    musician_released_amount,
    outstanding_musician_chargeback_debt,
    release_retained_payments,
)

pytestmark = pytest.mark.db

PRICE = Decimal("1000")
FEE = Decimal("2")


def _booking(db, *, status=BookingStatus.completed, musician=None, contractor=None, tag="a"):
    if musician is None:
        musician_user = User(email=f"m_{tag}@test.com", fullname="Músico", role=UserRole.musician)
        contractor_user = User(email=f"c_{tag}@test.com", fullname="Cliente", role=UserRole.contractor)
        db.add_all([musician_user, contractor_user])
        db.flush()
        musician = MusicianProfile(user_id=musician_user.id, stage_name="Banda Test")
        contractor = ContractorProfile(user_id=contractor_user.id)
        db.add_all([musician, contractor])
        db.flush()
    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=now_peru().date() - timedelta(days=1),
        start_time=time(20, 0),
        location_address="Av. Test 123",
        event_type="Boda",
        status=status,
        price_agreed=PRICE,
        platform_fee_percent=FEE,
    )
    db.add(booking)
    db.flush()
    return booking, musician, contractor


def test_chargeback_debt_is_deducted_automatically_and_complaint_refund_leaves_the_pool(db_session):
    booking, musician, contractor = _booking(db_session)
    gross = contractor_payable_total(booking)
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=gross,
            payment_type="full",
            status=PaymentStatus.retained,
            gateway_provider="mercadopago",
            gateway_payment_id="mp-pool-1",
        )
    )
    # Deuda previa del músico por un contracargo en OTRA reserva (D3).
    other, _, _ = _booking(db_session, musician=musician, contractor=contractor, tag="b")
    db_session.add(
        Payment(
            booking_id=other.id,
            amount=Decimal("500.00"),
            payment_type="full",
            status=PaymentStatus.refunded,
            gateway_metadata={"chargeback_after_release": True, "musician_chargeback_remaining": 300.0},
        )
    )
    db_session.commit()

    debt = outstanding_musician_chargeback_debt(db_session, musician.id)
    assert debt == Decimal("300.00")
    # Lo que se transfiere es el pool menos la deuda, nunca negativo.
    assert musician_net_payable(1000, debt) == 700.0
    assert musician_net_payable(200, debt) == 0.0

    # Queja resuelta: S/ 300 se reembolsan al contratista y S/ 700 quedan para el músico.
    release_retained_payments(
        db_session,
        booking.id,
        musician_amount=Decimal("700"),
        contractor_refund=Decimal("300"),
    )
    db_session.commit()

    # La deuda se descuenta de lo que se le asigna al músico (700), no del pool completo.
    assert outstanding_musician_chargeback_debt(db_session, musician.id) == Decimal("0.00")
    entries = {e.entry_type: e for e in db_session.query(LedgerEntry).filter_by(booking_id=booking.id)}
    assert Decimal(str(entries["musician_payout"].amount)) == Decimal("400.00")  # 700 - 300 de deuda
    assert Decimal(str(entries["platform_fee"].amount)) == gross - Decimal("1000.00")

    # Lo liberado al músico ya no incluye el reembolso: el pago original queda `released` por el bruto.
    complaint = BookingComplaint(
        booking_id=booking.id,
        opened_by_user_id=contractor.user_id,
        reason="No se presentó",
        status=BookingComplaintStatus.settled,
        refund_status=REFUND_STATUS_COMPLETED,
        admin_contractor_refund=Decimal("300"),
    )
    db_session.add(complaint)
    db_session.commit()
    assert musician_released_amount(booking, gross, complaint) == Decimal("700.00")
    assert musician_released_amount(booking, gross, None) == Decimal("1000.00")


def test_admin_can_settle_a_complaint_even_if_the_musician_never_answers(client, db_session):
    booking, musician, contractor = _booking(db_session)
    gross = contractor_payable_total(booking)
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=gross,
            payment_type="full",
            status=PaymentStatus.retained,
            gateway_provider="mercadopago",
            gateway_payment_id="mp-pool-2",
        )
    )
    db_session.add(
        BookingComplaint(
            booking_id=booking.id,
            opened_by_user_id=contractor.user_id,
            reason="Llegó tarde",
            status=BookingComplaintStatus.open,  # el músico nunca respondió
        )
    )
    admin = User(email="admin_settle@test.com", fullname="Admin", role=UserRole.admin)
    db_session.add(admin)
    db_session.commit()
    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin"))

    res = client.post(
        f"/api/v1/admin/settlements/{booking.id}/settle",
        json={"musician_amount": 1000.0, "contractor_refund": 0, "notes": "Sin descargo del músico"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["musician_net_payable"] == 1000.0
    assert body["musician_chargeback_debt"] == 0
    db_session.expire_all()
    complaint = db_session.query(BookingComplaint).filter_by(booking_id=booking.id).one()
    assert complaint.status == BookingComplaintStatus.settled
