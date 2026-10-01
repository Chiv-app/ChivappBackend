"""Read-model: unified contractor operations (act + inform + money)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

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
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.schemas.payment import ContractorOperationItem, ContractorOperationsSummary
from app.services.platform_payment import musician_portion_of_paid
from decimal import Decimal


# Pago único (100 %). Pagos antiguos con payment_type="advance" se muestran
# igual que "full"; cualquier otro tipo antiguo cae en "Pago".
_PAYMENT_TYPE_TITLE = {
    "full": "Pago total",
    "advance": "Pago total",
}

# status → (kind, op_status, title, subtitle, cta)
_LIFECYCLE: dict[BookingStatus, tuple[str, str, str, str, str]] = {
    BookingStatus.requested: (
        "awaiting_quote",
        "pending_other",
        "Esperando cotización",
        "El músico aún debe responder con una cotización.",
        "Ver reserva",
    ),
    BookingStatus.accepted: (
        "quote_review",
        "pending_me",
        "Revisar cotización",
        "Revisa el precio propuesto y acepta o rechaza.",
        "Revisar cotización",
    ),
    BookingStatus.contract_pending: (
        "contract_sign",
        "pending_me",
        "Firmar contrato y pagar",
        "Firma el contrato y paga el 100 % con Mercado Pago.",
        "Firmar contrato",
    ),
    BookingStatus.payment_pending: (
        "payment_review",
        "pending_other",
        "Pago en proceso",
        "Mercado Pago está confirmando tu pago.",
        "Ver reserva",
    ),
    BookingStatus.change_pending: (
        "change_pending",
        "pending_other",
        "Cambio en revisión",
        "Hay un cambio pendiente de aceptación.",
        "Ver reserva",
    ),
    BookingStatus.in_progress: (
        "event_active",
        "pending_me",
        "Evento en curso",
        "Comparte el show, reseña o finaliza la reserva.",
        "Continuar",
    ),
    BookingStatus.payment_released: (
        "finalize",
        "pending_me",
        "Finalizar reserva",
        "Puedes dejar reseña final o cerrar la reserva.",
        "Finalizar",
    ),
}

_STATUS_SORT = {
    "pending_me": 0,
    "pending_other": 1,
    "settled": 2,
    "done": 3,
    "rejected": 4,
    "cancelled": 5,
}


def _safe_occurred_at(*candidates: datetime | None) -> datetime:
    for value in candidates:
        if value is not None:
            return value
    return datetime.utcnow()


def _base_item(
    *,
    id: str,
    booking: Booking,
    musician_name: str | None,
    kind: str,
    status: str,
    direction: str,
    title: str,
    cta_label: str,
    occurred_at: datetime | None,
    source: str,
    subtitle: str | None = None,
    amount: float | None = None,
    currency: str = "PEN",
    payment_type: str | None = None,
    payment_status: PaymentStatus | None = None,
    complaint_status: str | None = None,
) -> ContractorOperationItem:
    return ContractorOperationItem(
        id=id,
        booking_id=booking.id,
        kind=kind,
        status=status,
        direction=direction,
        title=title,
        subtitle=subtitle,
        cta_label=cta_label,
        amount=amount,
        currency=currency,
        event_type=booking.event_type or "Evento",
        event_date=booking.event_date,
        location_city=booking.location_city,
        musician_stage_name=musician_name,
        booking_status=booking.status.value,
        payment_type=payment_type,
        payment_status=payment_status,
        complaint_status=complaint_status,
        occurred_at=_safe_occurred_at(occurred_at),
        source=source,
    )


def _payment_op_status(status: PaymentStatus) -> str:
    if status == PaymentStatus.initiated:
        return "pending_other"
    if status in {PaymentStatus.retained, PaymentStatus.released}:
        return "done"
    if status == PaymentStatus.refunded:
        return "settled"
    if status == PaymentStatus.failed:
        return "rejected"
    return "done"


def _payment_kind(payment_type: str | None) -> str:
    if payment_type in _PAYMENT_TYPE_TITLE:
        return "payment_full"
    return "payment_out"


def build_contractor_operations(
    db: Session,
    contractor_id: UUID,
) -> ContractorOperationsSummary:
    bookings = (
        db.query(Booking)
        .filter(Booking.contractor_id == contractor_id)
        .order_by(Booking.event_date.desc())
        .all()
    )

    total_quoted = 0.0
    bookings_active = 0
    bookings_completed = 0
    bookings_cancelled = 0
    booking_by_id: dict[UUID, Booking] = {}
    musician_ids: set[UUID] = set()

    for booking in bookings:
        booking_by_id[booking.id] = booking
        if booking.musician_id is not None:
            musician_ids.add(booking.musician_id)
        if booking.status == BookingStatus.cancelled:
            bookings_cancelled += 1
            continue
        if booking.status == BookingStatus.completed:
            bookings_completed += 1
        elif booking.status != BookingStatus.requested:
            bookings_active += 1
        if booking.price_agreed is not None:
            total_quoted += float(booking.price_agreed)

    musicians = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id.in_(musician_ids))
        .all()
        if musician_ids
        else []
    )
    musician_name_by_id = {m.id: m.stage_name for m in musicians}

    payments = (
        db.query(Payment)
        .filter(Payment.booking_id.in_(booking_by_id.keys()))
        .order_by(Payment.created_at.desc())
        .all()
        if booking_by_id
        else []
    )

    complaints = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id.in_(booking_by_id.keys()))
        .all()
        if booking_by_id
        else []
    )
    complaint_by_booking = {c.booking_id: c for c in complaints}

    items: list[ContractorOperationItem] = []
    total_released = 0.0
    total_retained = 0.0
    total_pending = 0.0
    total_out = 0.0
    total_in = 0.0
    total_service = 0.0
    total_fees = 0.0
    total_refund_pending = 0.0

    # --- Lifecycle / attention ops ---
    for booking in bookings:
        if booking.status == BookingStatus.cancelled:
            continue

        musician_name = musician_name_by_id.get(booking.musician_id)
        complaint = complaint_by_booking.get(booking.id)

        if complaint and complaint.status != BookingComplaintStatus.settled:
            status_label = {
                BookingComplaintStatus.open: "Disputa abierta",
                BookingComplaintStatus.musician_accepted:
                    "Disputa: el músico aceptó",
                BookingComplaintStatus.musician_responded:
                    "Disputa: respuesta del músico",
            }.get(complaint.status, "Disputa en curso")
            items.append(
                _base_item(
                    id=f"dispute:{complaint.id}",
                    booking=booking,
                    musician_name=musician_name,
                    kind="dispute",
                    status="pending_other",
                    direction="none",
                    title=status_label,
                    subtitle=(complaint.reason[:140] if complaint.reason else None),
                    cta_label="Ver disputa",
                    occurred_at=_safe_occurred_at(
                        complaint.updated_at, complaint.created_at
                    ),
                    source="settlement",
                    complaint_status=complaint.status.value,
                )
            )

        meta = _LIFECYCLE.get(booking.status)
        if meta:
            kind, op_status, title, subtitle, cta = meta
            amount = (
                float(booking.price_agreed)
                if booking.price_agreed is not None
                and kind in {"quote_review", "contract_sign"}
                else None
            )
            items.append(
                _base_item(
                    id=f"lifecycle:{booking.id}",
                    booking=booking,
                    musician_name=musician_name,
                    kind=kind,
                    status=op_status,
                    direction="none",
                    title=title,
                    subtitle=subtitle,
                    cta_label=cta,
                    amount=amount,
                    occurred_at=_safe_occurred_at(
                        booking.updated_at, booking.created_at
                    ),
                    source="booking",
                    complaint_status=(
                        complaint.status.value if complaint else None
                    ),
                )
            )

    # --- Payment movements (money out) — exclude refund ledger rows ---
    for payment in payments:
        booking = booking_by_id.get(payment.booking_id)
        if not booking:
            continue
        if (payment.payment_type or "").lower() == "refund":
            continue

        amount = float(payment.amount)
        service = float(musician_portion_of_paid(booking, Decimal(str(amount))))
        fee = round(max(amount - service, 0.0), 2)
        total_out += amount
        total_service += service
        total_fees += fee
        if payment.status == PaymentStatus.released:
            total_released += amount
        elif payment.status == PaymentStatus.retained:
            total_retained += amount
        elif payment.status == PaymentStatus.initiated:
            total_pending += amount

        type_label = _PAYMENT_TYPE_TITLE.get(
            payment.payment_type or "",
            payment.payment_type or "Pago",
        )
        status_hint = {
            PaymentStatus.initiated: "Comprobante en revisión",
            PaymentStatus.retained: "Retenido en garantía",
            PaymentStatus.released: "Liberado",
            PaymentStatus.refunded: "Reembolsado",
            PaymentStatus.failed: "Fallido",
        }.get(payment.status, payment.status.value)
        if fee > 0:
            status_hint = (
                f"{status_hint} · servicio S/ {service:.2f} · comisión S/ {fee:.2f}"
            )

        items.append(
            _base_item(
                id=f"payment:{payment.id}",
                booking=booking,
                musician_name=musician_name_by_id.get(booking.musician_id),
                kind=_payment_kind(payment.payment_type),
                status=_payment_op_status(payment.status),
                direction="out",
                title=type_label,
                subtitle=status_hint,
                cta_label="Ver reserva",
                amount=amount,
                currency=payment.currency or "PEN",
                payment_type=payment.payment_type,
                payment_status=payment.status,
                occurred_at=_safe_occurred_at(payment.created_at),
                source="payment",
            )
        )

    # --- Settlement refunds (money in) with transfer lifecycle ---
    for complaint in complaints:
        if complaint.status != BookingComplaintStatus.settled:
            continue
        if (
            complaint.admin_contractor_refund is None
            or float(complaint.admin_contractor_refund) <= 0
        ):
            continue
        booking = booking_by_id.get(complaint.booking_id)
        if not booking:
            continue
        refund_amount = float(complaint.admin_contractor_refund)
        rs = complaint.refund_status or REFUND_STATUS_NONE

        if rs == REFUND_STATUS_COMPLETED:
            op_status = "settled"
            title = "Devolución confirmada"
            subtitle = "Validaste el comprobante. Devolución completada."
            cta = "Ver reserva"
            total_in += refund_amount
        elif rs == REFUND_STATUS_AWAITING_VALIDATION:
            op_status = "pending_me"
            title = "Validar devolución"
            subtitle = "El admin envió el comprobante. Confirma si recibiste el dinero."
            cta = "Validar devolución"
            total_refund_pending += refund_amount
        elif rs == REFUND_STATUS_REJECTED:
            op_status = "pending_other"
            title = "Devolución rechazada · nueva transferencia"
            subtitle = (
                complaint.refund_rejection_reason
                or "Rechazaste el comprobante. El admin debe volver a transferir."
            )
            cta = "Ver disputa"
            total_refund_pending += refund_amount
        else:
            # awaiting_transfer / none
            op_status = "pending_other"
            title = "Devolución pendiente de transferencia"
            subtitle = (
                f"Liquidación definida por S/ {refund_amount:.2f}. "
                f"El admin aún debe enviar el comprobante."
            )
            cta = "Ver disputa"
            total_refund_pending += refund_amount

        items.append(
            _base_item(
                id=f"refund:{complaint.id}",
                booking=booking,
                musician_name=musician_name_by_id.get(booking.musician_id),
                kind="refund",
                status=op_status,
                direction="in",
                title=title,
                subtitle=subtitle,
                cta_label=cta,
                amount=refund_amount,
                occurred_at=_safe_occurred_at(
                    complaint.refund_validated_at,
                    complaint.refund_sent_at,
                    complaint.settled_at,
                    complaint.updated_at,
                    complaint.created_at,
                ),
                source="settlement",
                complaint_status=complaint.status.value,
            )
        )

    items.sort(
        key=lambda item: (
            _STATUS_SORT.get(item.status, 9),
            -(item.occurred_at.timestamp() if item.occurred_at else 0.0),
        )
    )

    pending_me_count = sum(
        1 for i in items if i.status == "pending_me" and i.source != "payment"
    )
    pending_other_count = sum(
        1 for i in items if i.status == "pending_other" and i.source != "payment"
    )
    dispute_count = sum(1 for i in items if i.kind in {"dispute", "refund"})

    return ContractorOperationsSummary(
        currency="PEN",
        total_out=total_out,
        total_in=total_in,
        net_out=total_out - total_in,
        total_quoted=total_quoted,
        total_service=total_service,
        total_fees=total_fees,
        total_released=total_released,
        total_retained=total_retained,
        total_pending=total_pending,
        total_refund_pending=total_refund_pending,
        pending_me_count=pending_me_count,
        pending_other_count=pending_other_count,
        dispute_count=dispute_count,
        bookings_active=bookings_active,
        bookings_completed=bookings_completed,
        bookings_cancelled=bookings_cancelled,
        items=items,
    )
