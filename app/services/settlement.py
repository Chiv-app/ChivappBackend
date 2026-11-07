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
    REFUND_STATUS_NEEDS_ATTENTION,
    REFUND_STATUS_NONE,
    REFUND_STATUS_PROCESSING,
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
            from app.services.booking_lifecycle import is_event_finished

            if (
                status
                in {
                    BookingStatus.payment_retained,
                    BookingStatus.in_progress,
                    BookingStatus.change_pending,
                    BookingStatus.payment_released,
                }
                and is_event_finished(booking)
            ):
                if complaint and complaint.refund_status == REFUND_STATUS_NEEDS_ATTENTION:
                    return "needs_attention"
                if complaint and complaint.status != BookingComplaintStatus.settled:
                    if complaint.status == BookingComplaintStatus.open:
                        return "disputed"
                    return "awaiting_admin"
                return "awaiting_reviews"
            return "in_progress"
        return "none"

    if complaint and complaint.refund_status == REFUND_STATUS_NEEDS_ATTENTION:
        return "needs_attention"

    if complaint and complaint.status != BookingComplaintStatus.settled:
        if complaint.status == BookingComplaintStatus.open:
            return "disputed"
        return "awaiting_admin"

    if retained > 0:
        return "payable"

    refund = complaint_refund_amount(complaint)
    if complaint and refund > 0:
        rs = complaint_refund_status(complaint)
        if rs == REFUND_STATUS_PROCESSING:
            return "awaiting_admin"
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


def _chargeback_debt_payments(db: Session, musician_id) -> list[Payment]:
    return (
        db.query(Payment)
        .join(Booking, Booking.id == Payment.booking_id)
        .filter(
            Booking.musician_id == musician_id,
            Payment.status == PaymentStatus.refunded,
            Payment.payment_type.is_distinct_from("refund"),
        )
        .order_by(Payment.created_at.asc())
        .all()
    )


def outstanding_musician_chargeback_debt(db: Session, musician_id) -> Decimal:
    """Deuda pendiente del músico por contracargos posteriores a una liberación (D3)."""
    if not musician_id:
        return Decimal("0.00")
    total = Decimal("0.00")
    for dp in _chargeback_debt_payments(db, musician_id):
        meta = dp.gateway_metadata if isinstance(dp.gateway_metadata, dict) else {}
        if not meta.get("chargeback_after_release"):
            continue
        remaining = Decimal(str(meta.get("musician_chargeback_remaining") or 0))
        if remaining > Decimal("0.00"):
            total += remaining
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def chargeback_deducted_for_booking(db: Session, booking_id) -> Decimal:
    """Descuento por contracargo ya aplicado al liberar los pagos de esta reserva."""
    total = Decimal("0.00")
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.released,
            Payment.payment_type.is_distinct_from("refund"),
        )
        .all()
    )
    for payment in rows:
        meta = payment.gateway_metadata if isinstance(payment.gateway_metadata, dict) else {}
        total += Decimal(str(meta.get("chargeback_deducted") or 0))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def musician_net_payable(pool: float | Decimal, debt: Decimal) -> float:
    """Lo que realmente se transfiere al músico: su parte menos la deuda por contracargo (nunca negativo)."""
    net = max(Decimal(str(pool or 0)) - debt, Decimal("0.00"))
    return float(net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def musician_released_amount(
    booking: Booking,
    released_gross: float | Decimal,
    complaint: BookingComplaint | None = None,
) -> Decimal:
    """Parte del músico ya liberada.

    Si hubo un reembolso por queja ejecutado, sale del pool del músico: se descuenta aquí
    porque el pago original queda `released` por el bruto completo.
    """
    base = musician_portion_of_paid(booking, Decimal(str(released_gross or 0)))
    refunded = Decimal("0.00")
    if complaint is not None and (complaint.refund_status or "") == REFUND_STATUS_COMPLETED:
        refunded = Decimal(str(complaint.admin_contractor_refund or 0))
    return max(base - refunded, Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def deduct_musician_chargeback_debt(
    db: Session,
    musician_id,
    max_deduction: Decimal,
) -> Decimal:
    """
    D3: Descuenta deuda pendiente por contracargo posterior a liberación
    del siguiente desembolso del músico.
    """
    if not musician_id or max_deduction <= Decimal("0.00"):
        return Decimal("0.00")

    debt_payments = _chargeback_debt_payments(db, musician_id)
    total_deducted = Decimal("0.00")
    remaining_budget = max_deduction
    for dp in debt_payments:
        if remaining_budget <= Decimal("0.00"):
            break
        meta = dict(dp.gateway_metadata) if isinstance(dp.gateway_metadata, dict) else {}
        if not meta.get("chargeback_after_release"):
            continue
        rem = Decimal(str(meta.get("musician_chargeback_remaining") or 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if rem <= Decimal("0.00"):
            continue
        applied = min(rem, remaining_budget)
        new_rem = (rem - applied).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        meta["musician_chargeback_remaining"] = float(new_rem)
        meta["musician_chargeback_last_deducted_at"] = datetime.utcnow().isoformat()
        dp.gateway_metadata = meta
        remaining_budget -= applied
        total_deducted += applied

    return total_deducted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def release_retained_payments(
    db: Session,
    booking_id,
    payout_reference: str | None = None,
    payout_evidence_url: str | None = None,
    payout_notes: str | None = None,
    musician_amount: Decimal | None = None,
    contractor_refund: Decimal | None = None,
) -> list[Payment]:
    """Libera los pagos retenidos.

    `musician_amount` es lo que el admin asigna al músico (tope del descuento por contracargo);
    `contractor_refund` es lo ya reembolsado al contratista por queja y sale del pool del músico.
    """
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
    booking = db.get(Booking, booking_id) if payments else None
    deducted_total = Decimal("0.00")
    if booking and booking.musician_id and payments:
        gross_releasing = sum(
            (Decimal(str(p.amount or 0)) for p in payments if (p.payment_type or "").lower() != "refund"),
            Decimal("0.00"),
        )
        musician_releasing = musician_portion_of_paid(booking, gross_releasing)
        if musician_amount is not None:
            musician_releasing = min(musician_releasing, Decimal(str(musician_amount)))
        deducted_total = deduct_musician_chargeback_debt(
            db, booking.musician_id, musician_releasing
        )

    for idx, payment in enumerate(payments):
        payment.status = PaymentStatus.released
        payment.released_at = now
        if payout_reference:
            payment.payout_reference = payout_reference
        if payout_evidence_url:
            payment.payout_evidence_url = payout_evidence_url
        effective_notes = payout_notes
        if idx == 0 and deducted_total > Decimal("0.00"):
            meta_dict = dict(payment.gateway_metadata) if isinstance(payment.gateway_metadata, dict) else {}
            meta_dict["chargeback_deducted"] = float(deducted_total)
            payment.gateway_metadata = meta_dict
            deduct_note = f"[Descuento por contracargo previo: S/ {float(deducted_total):.2f}]"
            effective_notes = f"{payout_notes} {deduct_note}".strip() if payout_notes else deduct_note
        if effective_notes:
            payment.payout_notes = effective_notes

    if payments:
        db.flush()
        try:
            from app.services.ledger import record_ledger_entry

            gross_released = sum(
                (Decimal(str(p.amount or 0)) for p in payments if (p.payment_type or "").lower() != "refund"),
                Decimal("0.00"),
            )
            musician_net = (
                musician_portion_of_paid(booking, gross_released)
                if booking
                else gross_released
            )
            if contractor_refund is not None and Decimal(str(contractor_refund)) > Decimal("0.00"):
                musician_net = max(musician_net - Decimal(str(contractor_refund)), Decimal("0.00"))
            effective_payout = max(musician_net - deducted_total, Decimal("0.00"))
            record_ledger_entry(
                db,
                entry_type="musician_payout",
                amount=effective_payout,
                booking_id=booking_id,
                payment_id=payments[0].id,
                description=f"Liberación de pago al músico ({payout_reference or 'manual'})",
                meta={
                    "gross_released": float(gross_released),
                    "musician_portion": float(musician_net),
                    "chargeback_deducted": float(deducted_total),
                    "payout_reference": payout_reference,
                },
            )
            platform_portion = max(
                gross_released
                - (musician_net + Decimal(str(contractor_refund or 0))),
                Decimal("0.00"),
            )
            if platform_portion > Decimal("0.00"):
                record_ledger_entry(
                    db,
                    entry_type="platform_fee",
                    amount=platform_portion,
                    booking_id=booking_id,
                    payment_id=payments[0].id,
                    description="Comisión de plataforma y pasarela retenida en liquidación",
                )
        except Exception:
            pass
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
