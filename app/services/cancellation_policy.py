"""Política de cancelación con reembolso automático por Mercado Pago.

Reglas (configurables en settings):
- Cancela el contratista con el pago retenido: según días de anticipación al evento,
  se reembolsa un % de lo pagado SIN el costo de la pasarela.
    > CANCEL_FULL_REFUND_MIN_DAYS días            -> 100 %
    >= CANCEL_PARTIAL_REFUND_MIN_DAYS días         -> CANCEL_PARTIAL_REFUND_PERCENT %
    menos                                          -> 0 %
- Cancela el músico: 100 % de lo pagado (incluido el costo de pasarela).
- Cancela el admin: el % que indique (por defecto 100 %) sobre lo pagado.

Ningún reembolso se ejecuta sin aprobación del admin (pending_approval -> processing).
Lo que no se reembolsa queda retenido y el admin lo liquida al músico.
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
    # Legacy (unreachable) statuses from the old advance/balance flow.
    BookingStatus.balance_pending,
    BookingStatus.balance_review,
}

CANCELLABLE_STATUSES = {
    BookingStatus.requested,
    BookingStatus.accepted,
    BookingStatus.contract_pending,
    BookingStatus.contract_signed,
    BookingStatus.payment_pending,
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


def _without_gateway_cost(booking: Booking, gross: Decimal) -> Decimal:
    """Parte de `gross` que corresponde a precio + comisión de plataforma."""
    from app.services.platform_payment import contractor_payable_total

    total = contractor_payable_total(booking)
    if not total or total <= 0 or booking.price_agreed is None:
        return gross
    price = Decimal(str(booking.price_agreed))
    percent = Decimal(str(booking.platform_fee_percent or 0))
    app_total = price * (Decimal("1") + percent / Decimal("100"))
    return min(gross, gross * app_total / total)


def contractor_refund_percent(days_before_event: int) -> tuple[int, str]:
    full_days = settings.CANCEL_FULL_REFUND_MIN_DAYS
    partial_days = settings.CANCEL_PARTIAL_REFUND_MIN_DAYS
    partial = settings.CANCEL_PARTIAL_REFUND_PERCENT
    if days_before_event > full_days:
        return 100, f"Más de {full_days} días de anticipación: reembolso del 100 %"
    if days_before_event >= partial_days:
        return partial, f"Entre {partial_days} y {full_days} días de anticipación: reembolso del {partial} %"
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

    if cancelled_by == "musician":
        percent, base = 100, paid
        rule = "Cancelación del músico: reembolso del 100 % al contratista"
    elif cancelled_by == "admin":
        percent = 100 if admin_refund_percent is None else admin_refund_percent
        base = paid
        rule = f"Cancelación de la plataforma: reembolso del {percent} %"
    else:
        percent, rule = contractor_refund_percent(days)
        base = _without_gateway_cost(booking, paid)

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
    """El admin aprueba el reembolso (opcionalmente ajustando el monto) y se ejecuta.

    `amount=0` rechaza el reembolso: todo lo retenido queda para liquidar al músico.
    """
    paid = sum((Decimal(str(p.amount)) for p in _retained_payments(db, booking)), Decimal("0"))
    if amount is not None:
        if amount < 0 or amount > paid:
            raise ValueError(f"El monto debe estar entre 0 y lo retenido (S/ {paid:.2f}).")
        booking.cancellation_refund_amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        booking.cancellation_refund_percent = (
            (amount / paid * 100).quantize(CENT, rounding=ROUND_HALF_UP) if paid > 0 else Decimal("0")
        )

    if Decimal(str(booking.cancellation_refund_amount or 0)) <= 0:
        booking.cancellation_refund_status = REFUND_REJECTED
        db.commit()
        return

    booking.cancellation_refund_status = REFUND_PROCESSING
    db.commit()
    execute_cancellation_refund(db, booking)


def execute_cancellation_refund(db: Session, booking: Booking) -> None:
    """Reembolsa por Mercado Pago lo pendiente de `cancellation_refund_amount`.

    Se llama DESPUÉS de hacer commit de la cancelación, para que un fallo de la
    pasarela deje un registro reintentable. Las claves de idempotencia son
    deterministas: un reintento no reembolsa dos veces. No lanza excepciones.
    """
    from app.services.mercadopago_service import issue_refund

    target = Decimal(str(booking.cancellation_refund_amount or 0))
    pending = target - _already_refunded(db, booking)
    if pending <= 0:
        booking.cancellation_refund_status = REFUND_COMPLETED if target > 0 else REFUND_NONE
        db.commit()
        return

    try:
        for payment in _retained_payments(db, booking):
            if pending <= 0:
                break
            if not payment.gateway_payment_id:
                raise ValueError(
                    "Hay un pago sin Mercado Pago (comprobante manual): el reembolso debe hacerse por transferencia."
                )
            portion = min(pending, Decimal(str(payment.amount))).quantize(CENT, rounding=ROUND_HALF_UP)
            response = issue_refund(
                payment.gateway_payment_id,
                float(portion),
                idempotency_key=f"cancel-{booking.id}-{payment.id}-{portion}",
            )
            remaining = Decimal(str(payment.amount)) - portion
            if remaining <= 0:
                payment.status = PaymentStatus.refunded
            else:
                payment.amount = remaining
            db.add(
                Payment(
                    booking_id=booking.id,
                    amount=portion,
                    currency=payment.currency or "PEN",
                    payment_type="refund",
                    status=PaymentStatus.refunded,
                    gateway_provider="mercadopago",
                    gateway_payment_id=payment.gateway_payment_id,
                    gateway_metadata={"refund": response, "original_payment_id": str(payment.id)},
                )
            )
            # Commit por pago: si el siguiente falla, este ya quedó registrado.
            db.commit()
            pending -= portion

        if pending > 0:
            raise ValueError("No hay pagos retenidos suficientes para completar el reembolso.")
        booking.cancellation_refund_status = REFUND_COMPLETED
        booking.cancellation_refunded_at = datetime.utcnow()
        booking.cancellation_refund_error = None
    except Exception as exc:  # noqa: BLE001 - se registra y se reintenta desde admin
        db.rollback()
        logger.error("Reembolso de cancelación fallido para reserva %s: %s", booking.id, exc)
        booking.cancellation_refund_status = REFUND_FAILED
        booking.cancellation_refund_error = str(exc)[:500]
    db.commit()
