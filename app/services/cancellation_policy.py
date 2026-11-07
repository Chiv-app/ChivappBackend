"""Política de cancelación con reembolso automático por Mercado Pago.

Lo que paga el contratista = tarifa del músico + comisión de la plataforma + costo de
la pasarela. El reembolso SOLO sale de la tarifa del músico; la comisión y la pasarela
nunca se devuelven, cancele quien cancele.

Reglas (configurables en settings), sobre la tarifa del músico:
- Cancela el contratista con el pago retenido, según días de anticipación al evento:
    >= CANCEL_FULL_REFUND_MIN_DAYS días            -> 100 %
    >= CANCEL_PARTIAL_REFUND_MIN_DAYS días         -> CANCEL_PARTIAL_REFUND_PERCENT %
    menos                                          -> 0 %
- Cancela el músico: 100 % de la tarifa.
- Cancela el admin: el % que indique (por defecto 100 %) de la tarifa.

Ningún reembolso se ejecuta sin aprobación del admin (pending_approval -> processing).
La parte de la tarifa que no se reembolsa se liquida al músico; la comisión y la
pasarela quedan en la plataforma (ver `musician_portion_of_paid`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")

# Estados con el dinero ya retenido por la plataforma.
RETAINED_STATUSES = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.in_progress,
}

CANCELLABLE_STATUSES = {
    BookingStatus.requested,
    BookingStatus.accepted,
    BookingStatus.contract_pending,
    BookingStatus.contract_signed,
} | RETAINED_STATUSES

REFUND_PENDING_APPROVAL = "pending_approval"  # calculado, espera aprobación del admin
REFUND_PROCESSING = "processing"
REFUND_COMPLETED = "completed"
REFUND_FAILED = "failed"
REFUND_REJECTED = "rejected"  # el admin decidió no reembolsar
REFUND_NONE = "none"

# Mientras el reembolso no esté resuelto, no se liquida nada al músico.
REFUND_UNRESOLVED = {REFUND_PENDING_APPROVAL, REFUND_PROCESSING, REFUND_FAILED}


@dataclass
class CancellationQuote:
    cancelled_by: str
    days_before_event: int
    paid_total: Decimal
    refundable_base: Decimal
    refund_percent: int
    refund_amount: Decimal
    rule: str


def days_until_event(booking: Booking) -> int:
    return (booking.event_date - now_peru().date()).days


def _retained_payments(db: Session, booking: Booking) -> list[Payment]:
    rows = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.status == PaymentStatus.retained,
        )
        .order_by(Payment.amount.desc())
        .all()
    )
    return [p for p in rows if (p.payment_type or "").lower() != "refund"]


def _refundable_base(booking: Booking, paid: Decimal) -> Decimal:
    """Base reembolsable: solo la tarifa del músico.

    La comisión de la plataforma y el costo de la pasarela forman parte de lo que
    pagó el contratista pero nunca se reembolsan.
    """
    if booking.price_agreed is None:
        return paid
    return min(paid, Decimal(str(booking.price_agreed)))


def parse_frozen_cancellation_policy(raw: str | None) -> tuple[int, int, int] | None:
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        full_days = int(parts[0].rstrip("dD"))
        partial_days = int(parts[1].rstrip("dD"))
        partial_pct = int(parts[2].rstrip("%"))
    except ValueError:
        return None
    if full_days <= 0 or partial_days < 0 or not (0 <= partial_pct <= 100):
        return None
    return full_days, partial_days, partial_pct


def _resolve_booking_cancellation_policy(
    db: Session | None, booking: Booking | None
) -> tuple[int, int, int]:
    if db is not None and booking is not None and getattr(booking, "id", None) is not None:
        from app.models.contract import Contract

        contract = (
            db.query(Contract)
            .filter(Contract.booking_id == booking.id)
            .first()
        )
        if contract and isinstance(contract.context, dict):
            frozen = parse_frozen_cancellation_policy(
                contract.context.get("__cancellation_policy__")
            )
            if frozen is not None:
                return frozen
    return (
        int(settings.CANCEL_FULL_REFUND_MIN_DAYS),
        int(settings.CANCEL_PARTIAL_REFUND_MIN_DAYS),
        int(settings.CANCEL_PARTIAL_REFUND_PERCENT),
    )


def contractor_refund_percent(
    days_before_event: int,
    *,
    full_days: int | None = None,
    partial_days: int | None = None,
    partial_percent: int | None = None,
) -> tuple[int, str]:
    full_days = settings.CANCEL_FULL_REFUND_MIN_DAYS if full_days is None else full_days
    partial_days = settings.CANCEL_PARTIAL_REFUND_MIN_DAYS if partial_days is None else partial_days
    partial = settings.CANCEL_PARTIAL_REFUND_PERCENT if partial_percent is None else partial_percent
    if days_before_event >= full_days:
        return 100, f"{full_days} días o más de anticipación: reembolso del 100 %"
    if days_before_event >= partial_days:
        return partial, f"Entre {partial_days} y {full_days - 1} días de anticipación: reembolso del {partial} %"
    return 0, f"Menos de {partial_days} días de anticipación: sin reembolso"


def quote_cancellation(
    db: Session,
    booking: Booking,
    cancelled_by: str,
    admin_refund_percent: int | None = None,
) -> CancellationQuote:
    days = days_until_event(booking)
    paid = sum((Decimal(str(p.amount)) for p in _retained_payments(db, booking)), Decimal("0"))

    if paid <= 0:
        return CancellationQuote(cancelled_by, days, Decimal("0"), Decimal("0"), 0, Decimal("0"), "Sin pagos retenidos")

    base = _refundable_base(booking, paid)
    if cancelled_by == "musician":
        percent = 100
        rule = "Cancelación del músico: reembolso del 100 % de la tarifa del artista"
    elif cancelled_by == "admin":
        percent = 100 if admin_refund_percent is None else admin_refund_percent
        rule = f"Cancelación de la plataforma: reembolso del {percent} % de la tarifa del artista"
    else:
        full_d, partial_d, partial_pct = _resolve_booking_cancellation_policy(db, booking)
        percent, rule = contractor_refund_percent(
            days,
            full_days=full_d,
            partial_days=partial_d,
            partial_percent=partial_pct,
        )

    amount = (base * Decimal(percent) / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    return CancellationQuote(
        cancelled_by=cancelled_by,
        days_before_event=days,
        paid_total=paid.quantize(CENT, rounding=ROUND_HALF_UP),
        refundable_base=base.quantize(CENT, rounding=ROUND_HALF_UP),
        refund_percent=percent,
        refund_amount=min(amount, paid),
        rule=rule,
    )


def _already_refunded(db: Session, booking: Booking) -> Decimal:
    rows = (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id, Payment.payment_type == "refund")
        .all()
    )
    return sum((Decimal(str(p.amount)) for p in rows), Decimal("0"))


def record_cancellation(booking: Booking, quote: CancellationQuote, *, approved: bool = False) -> None:
    """Cancela la reserva y deja el reembolso calculado.

    Ningún reembolso sale sin aprobación del admin: si cancela un participante,
    queda en `pending_approval`. Si cancela el admin (`approved=True`), su
    decisión ya es la aprobación y queda listo para ejecutarse.
    """
    booking.cancelled_by = quote.cancelled_by
    booking.cancelled_at = datetime.utcnow()
    booking.status = BookingStatus.cancelled
    booking.cancellation_refund_percent = Decimal(quote.refund_percent)
    booking.cancellation_refund_amount = quote.refund_amount
    if quote.refund_amount <= 0:
        booking.cancellation_refund_status = REFUND_NONE
    else:
        booking.cancellation_refund_status = REFUND_PROCESSING if approved else REFUND_PENDING_APPROVAL
    booking.cancellation_refund_error = None


def approve_cancellation_refund(db: Session, booking: Booking, amount: Decimal | None) -> None:
    """El admin aprueba el reembolso (opcionalmente ajustando el monto).

    Mercado Pago solo cobra: aprobar deja el reembolso en `processing` (aprobado, falta que el
    admin transfiera y lo registre con `complete_manual_cancellation_refund`).
    `amount=0` rechaza el reembolso: todo lo retenido queda para liquidar al músico.
    """
    paid = sum((Decimal(str(p.amount)) for p in _retained_payments(db, booking)), Decimal("0"))
    max_refund = _refundable_base(booking, paid)
    if amount is not None:
        if amount < 0 or amount > max_refund:
            raise ValueError(
                f"El monto debe estar entre 0 y la tarifa reembolsable (S/ {max_refund:.2f}). "
                "La comisión y el costo de la pasarela no se reembolsan."
            )
        booking.cancellation_refund_amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        booking.cancellation_refund_percent = (
            (amount / max_refund * 100).quantize(CENT, rounding=ROUND_HALF_UP) if max_refund > 0 else Decimal("0")
        )

    if Decimal(str(booking.cancellation_refund_amount or 0)) <= 0:
        booking.cancellation_refund_status = REFUND_REJECTED
        db.commit()
        return

    booking.cancellation_refund_status = REFUND_PROCESSING
    db.commit()


def complete_manual_cancellation_refund(
    db: Session,
    booking: Booking,
    *,
    reference: str,
    evidence_url: str | None = None,
    notes: str | None = None,
) -> Decimal:
    """Registra que el admin ya transfirió el reembolso de una cancelación.

    Mercado Pago solo cobra: no se llama a ninguna pasarela. Aquí solo se deja la contabilidad
    (filas `refund`, asientos y estado) y la referencia de la operación. Todo ocurre en una sola
    transacción: si algo falla no queda nada a medias. Devuelve el monto registrado.
    """
    from app.services.ledger import record_ledger_entry

    reference = (reference or "").strip()
    if not reference:
        raise ValueError("Indica la referencia de la transferencia.")

    target = Decimal(str(booking.cancellation_refund_amount or 0))
    if booking.price_agreed is not None:
        # Nunca se reembolsa más que la tarifa del músico (reservas anteriores a la regla actual).
        cap = Decimal(str(booking.price_agreed))
        if target > cap:
            target = cap.quantize(CENT, rounding=ROUND_HALF_UP)
            booking.cancellation_refund_amount = target
    if target <= 0:
        raise ValueError("Esta cancelación no tiene un monto por reembolsar.")

    pending = target - _already_refunded(db, booking)
    if pending <= 0:
        raise ValueError("Este reembolso ya fue registrado.")

    registered = Decimal("0")
    try:
        for payment in _retained_payments(db, booking):
            if pending <= 0:
                break
            portion = min(pending, Decimal(str(payment.amount))).quantize(CENT, rounding=ROUND_HALF_UP)
            remaining = Decimal(str(payment.amount)) - portion
            if remaining <= 0:
                payment.status = PaymentStatus.refunded
            else:
                payment.amount = remaining
            refund_row = Payment(
                booking_id=booking.id,
                amount=portion,
                currency=payment.currency or "PEN",
                payment_type="refund",
                status=PaymentStatus.refunded,
                gateway_provider="manual",
                payout_reference=reference,
                payout_evidence_url=(evidence_url or None),
                payout_notes=(notes or None),
                gateway_metadata={"manual": True, "original_payment_id": str(payment.id)},
            )
            db.add(refund_row)
            db.flush()
            record_ledger_entry(
                db,
                entry_type="refund",
                amount=portion,
                booking_id=booking.id,
                payment_id=refund_row.id,
                currency=payment.currency or "PEN",
                description="Reembolso manual por cancelación de reserva",
                meta={"original_payment_id": str(payment.id), "reference": reference},
            )
            pending -= portion
            registered += portion

        if pending > 0:
            raise ValueError("No hay pagos retenidos suficientes para completar el reembolso.")
        booking.cancellation_refund_status = REFUND_COMPLETED
        booking.cancellation_refunded_at = datetime.utcnow()
        booking.cancellation_refund_error = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    return registered
