from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.timezone import now_peru_naive

from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.booking_complaint import (
    REFUND_STATUS_AWAITING_TRANSFER,
    REFUND_STATUS_AWAITING_VALIDATION,
    REFUND_STATUS_COMPLETED,
    REFUND_STATUS_NONE,
    REFUND_STATUS_REJECTED,
    BookingComplaint,
    BookingComplaintStatus,
)
from app.models.payment import Payment, PaymentStatus
from app.services.platform_payment import musician_portion_of_paid


def retained_total_for_booking(db: Session, booking_id) -> float:
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.retained,
        )
        .all()
    )
    return sum(float(p.amount) for p in rows)


def released_total_for_booking(db: Session, booking_id) -> float:
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.released,
        )
        .all()
    )
    return sum(float(p.amount) for p in rows)


def paid_total_for_booking(db: Session, booking_id) -> float:
    """Gross contractor deposits (advance/full/balance), excluding refund rows."""
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status.in_(
                [
                    PaymentStatus.initiated,
                    PaymentStatus.retained,
                    PaymentStatus.released,
                ]
            ),
        )
        .all()
    )
    total = 0.0
    for payment in rows:
        if (payment.payment_type or "").lower() == "refund":
            continue
        total += float(payment.amount)
    return total


def complaint_refund_amount(complaint: BookingComplaint | None) -> float:
    if not complaint or complaint.admin_contractor_refund is None:
        return 0.0
    return float(complaint.admin_contractor_refund)


def complaint_refund_status(complaint: BookingComplaint | None) -> str:
    if not complaint:
        return REFUND_STATUS_NONE
    return complaint.refund_status or REFUND_STATUS_NONE


def serialize_complaint(complaint: BookingComplaint | None):
    if not complaint:
        return None
    from app.schemas.settlement import BookingComplaintOut

    return BookingComplaintOut(
        id=complaint.id,
        booking_id=complaint.booking_id,
        opened_by_user_id=complaint.opened_by_user_id,
        reason=complaint.reason,
        evidence_url=complaint.evidence_url,
        status=complaint.status,
        musician_response=complaint.musician_response,
        musician_response_evidence_url=complaint.musician_response_evidence_url,
        musician_responded_at=complaint.musician_responded_at,
        admin_musician_amount=(
            float(complaint.admin_musician_amount)
            if complaint.admin_musician_amount is not None
            else None
        ),
        admin_contractor_refund=(
            float(complaint.admin_contractor_refund)
            if complaint.admin_contractor_refund is not None
            else None
        ),
        admin_notes=complaint.admin_notes,
        settled_at=complaint.settled_at,
        refund_status=complaint_refund_status(complaint),
        refund_evidence_url=complaint.refund_evidence_url,
        refund_sent_at=complaint.refund_sent_at,
        refund_validated_at=complaint.refund_validated_at,
        refund_rejection_reason=complaint.refund_rejection_reason,
        refund_payment_id=complaint.refund_payment_id,
        created_at=complaint.created_at,
        updated_at=complaint.updated_at,
    )


def settlement_state_for_booking(
    booking: Booking,
    complaint: BookingComplaint | None,
    retained: float,
    released: float,
) -> str:
    if booking.status == BookingStatus.cancelled:
        from app.services.cancellation_policy import REFUND_UNRESOLVED

        # Primero se resuelve el reembolso; luego lo no reembolsado se liquida al músico.
        if booking.cancellation_refund_status in REFUND_UNRESOLVED:
            return "awaiting_admin"
        if retained > 0:
            return "payable"
        return "settled" if released > 0 else "none"

    if booking.status != BookingStatus.completed:
        if retained > 0:
            return "in_progress"
        return "none"

    if complaint and complaint.status != BookingComplaintStatus.settled:
        if complaint.status == BookingComplaintStatus.open:
            return "disputed"
        return "awaiting_admin"

    if retained > 0:
        return "payable"

    refund = complaint_refund_amount(complaint)
    if complaint and refund > 0:
        rs = complaint_refund_status(complaint)
        if rs in {
            REFUND_STATUS_NONE,
            REFUND_STATUS_AWAITING_TRANSFER,
            REFUND_STATUS_REJECTED,
        }:
            return "refund_pending_transfer"
        if rs == REFUND_STATUS_AWAITING_VALIDATION:
            return "refund_pending_validation"
        if rs == REFUND_STATUS_COMPLETED:
            return "settled"

    if released > 0:
        return "settled"
    return "settled"


def release_retained_payments(
    db: Session,
    booking_id,
    payout_reference: str | None = None,
    payout_evidence_url: str | None = None,
    payout_notes: str | None = None,
) -> list[Payment]:
    now = now_peru_naive()
    payments = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.retained,
        )
        .all()
    )
    for payment in payments:
        payment.status = PaymentStatus.released
        payment.released_at = now
        if payout_reference:
            payment.payout_reference = payout_reference
        if payout_evidence_url:
            payment.payout_evidence_url = payout_evidence_url
        if payout_notes:
            payment.payout_notes = payout_notes
    return payments



def musician_pool_from_gross(booking: Booking, gross: float) -> float:
    return float(musician_portion_of_paid(booking, Decimal(str(gross))))


def fee_portion_from_gross(booking: Booking, gross: float) -> float:
    pool = musician_pool_from_gross(booking, gross)
    return round(max(gross - pool, 0.0), 2)
