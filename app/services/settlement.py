from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from app.core.timezone import now_peru_naive

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, object_session

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


def _round_money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def retained_total_for_booking(db: Session, booking_id) -> float:
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.retained,
            Payment.payment_type.is_distinct_from("refund"),
        )
        .all()
    )
    total = sum((Decimal(str(p.amount or 0)) for p in rows), Decimal("0.00"))
    return _round_money(total)


def released_total_for_booking(db: Session, booking_id) -> float:
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.released,
            Payment.payment_type.is_distinct_from("refund"),
        )
        .all()
    )
    total = sum((Decimal(str(p.amount or 0)) for p in rows), Decimal("0.00"))
    return _round_money(total)


def paid_total_for_booking(db: Session, booking_id) -> float:
    """Gross contractor deposits (single full payment), excluding refund rows."""
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
            Payment.payment_type.is_distinct_from("refund"),
        )
        .all()
    )
    total = Decimal("0.00")
    for payment in rows:
        if (payment.payment_type or "").lower() == "refund":
            continue
        total += Decimal(str(payment.amount or 0))
    return _round_money(total)


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


def booking_has_gateway_mediation(booking: Booking, db: Session | None = None) -> bool:
    sess = db or object_session(booking)
    if sess is None or getattr(booking, "id", None) is None:
        return False
    rows = (
        sess.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.status.in_([PaymentStatus.retained, PaymentStatus.released]),
            Payment.payment_type.is_distinct_from("refund"),
        )
        .all()
    )
    for p in rows:
        meta = p.gateway_metadata if isinstance(p.gateway_metadata, dict) else {}
        if meta.get("in_mediation"):
            return True
    return False


def settlement_state_for_booking(
    booking: Booking,
    complaint: BookingComplaint | None,
    retained: float,
    released: float,
    *,
    effective_status: BookingStatus | None = None,
) -> str:
    status = effective_status or booking.status
    if booking_has_gateway_mediation(booking):
        return "disputed"
    if status == BookingStatus.cancelled:
        from app.services.cancellation_policy import REFUND_UNRESOLVED

        # Primero se resuelve el reembolso; luego lo no reembolsado se liquida al músico.
        if booking.cancellation_refund_status in REFUND_UNRESOLVED:
            return "awaiting_admin"
        if retained > 0:
            return "payable"
        return "settled" if released > 0 else "none"

    if status != BookingStatus.completed:
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
    now = datetime.utcnow()
    payments = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.retained,
        )
        .all()
    )
    for payment in payments:
        meta = payment.gateway_metadata if isinstance(payment.gateway_metadata, dict) else {}
        if meta.get("in_mediation"):
            raise ValueError(
                "No se puede liberar el pago mientras exista una mediación activa en la pasarela."
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

    if payments:
        db.flush()
        try:
            from app.models.payout_request import PayoutRequest

            booking = db.get(Booking, booking_id)
            if booking and booking.musician_id:
                remaining_retained = (
                    db.query(Payment.id)
                    .join(Booking, Booking.id == Payment.booking_id)
                    .filter(
                        Booking.musician_id == booking.musician_id,
                        Booking.status.in_(
                            [BookingStatus.completed, BookingStatus.cancelled]
                        ),
                        Payment.status == PaymentStatus.retained,
                        Payment.payment_type.is_distinct_from("refund"),
                    )
                    .first()
                )
                if not remaining_retained:
                    pending_requests = (
                        db.query(PayoutRequest)
                        .filter(
                            PayoutRequest.musician_id == booking.musician_id,
                            PayoutRequest.status == "pending",
                        )
                        .all()
                    )
                    for req in pending_requests:
                        req.status = "processed"
                        req.processed_at = now
        except SQLAlchemyError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "No se pudieron actualizar solicitudes de retiro pendientes para músico %s: %s",
                booking.musician_id,
                exc,
            )

    return payments



def musician_pool_from_gross(booking: Booking, gross: float) -> float:
    return float(musician_portion_of_paid(booking, Decimal(str(gross))))


def fee_portion_from_gross(booking: Booking, gross: float) -> float:
    pool = musician_pool_from_gross(booking, gross)
    return round(max(gross - pool, 0.0), 2)
