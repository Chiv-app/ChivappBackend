"""Send post-event review reminders and expire stale bookings pending response or payment."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.email_log import EmailLog
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.services.booking_notifications import notify_user
from app.services.email.service import send_templated_email

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REQUESTED_EXPIRY_HOURS = 72
REQUESTED_WARNING_HOURS = 48
UNPAID_EXPIRY_DAYS = 7
UNPAID_WARNING_DAYS = 6
CHANGE_PENDING_EXPIRY_HOURS = 48


def _has_notification(db: Session, user_id, notif_type: str, booking_id: str) -> bool:
    rows = (
        db.query(Notification)
        .filter(
            Notification.user_id == user_id,
            Notification.type == notif_type,
        )
        .all()
    )
    return any((r.meta or {}).get("booking_id") == booking_id for r in rows)


def expire_stale_bookings(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Expira reservas estancadas según los plazos de D4 y envía avisos 24 h antes."""
    now_utc = (now or datetime.utcnow()).replace(tzinfo=None)
    stats = {
        "expired_requested": 0,
        "expired_unpaid": 0,
        "warned_requested": 0,
        "warned_unpaid": 0,
    }

    # 1. Reservas en `requested` (72 h sin respuesta del músico; aviso a las 48 h)
    requested_bookings = (
        db.query(Booking)
        .options(joinedload(Booking.contractor), joinedload(Booking.musician))
        .filter(Booking.status == BookingStatus.requested)
        .all()
    )
    for b in requested_bookings:
        ref_dt = (b.created_at or now_utc).replace(tzinfo=None)
        age = now_utc - ref_dt
        b_id = str(b.id)
        contractor_user = b.contractor.user if b.contractor else None
        musician_user = b.musician.user if b.musician else None

        if age >= timedelta(hours=REQUESTED_EXPIRY_HOURS):
            b.status = BookingStatus.cancelled
            b.cancelled_by = "system"
            b.cancelled_at = now_utc
            b.rejection_reason = "Expirada por falta de respuesta del músico en 72 horas"
            if contractor_user:
                notify_user(
                    db,
                    user=contractor_user,
                    type="booking_expired",
                    title="Solicitud de reserva expirada",
                    message="La solicitud expiró tras 72 horas sin respuesta del músico.",
                    booking_id=b_id,
                )
            if musician_user:
                notify_user(
                    db,
                    user=musician_user,
                    type="booking_expired",
                    title="Solicitud de reserva expirada",
                    message="Una solicitud pendiente expiró por falta de cotización en 72 horas.",
                    booking_id=b_id,
                )
            stats["expired_requested"] += 1
        elif age >= timedelta(hours=REQUESTED_WARNING_HOURS):
            if musician_user and not _has_notification(
                db, musician_user.id, "booking_expiration_warning", b_id
            ):
                notify_user(
                    db,
                    user=musician_user,
                    type="booking_expiration_warning",
                    title="Solicitud por expirar en 24 horas",
                    message="Tienes una solicitud pendiente que expirará en menos de 24 horas si no envías cotización.",
                    booking_id=b_id,
                )
                stats["warned_requested"] += 1

    # 2. Reservas sin pago en `accepted`, `contract_pending`, `contract_signed`, `payment_pending` (7 días; aviso a los 6 días)
    unpaid_statuses = [
        BookingStatus.accepted,
        BookingStatus.contract_pending,
        BookingStatus.contract_signed,
        BookingStatus.payment_pending,
    ]
    unpaid_bookings = (
        db.query(Booking)
        .options(joinedload(Booking.contractor), joinedload(Booking.musician))
        .filter(Booking.status.in_(unpaid_statuses))
        .all()
    )
    # Consulta por lote de pagos para todas las reservas candidatas (evita N+1).
    payments_by_booking: dict = {}
    if unpaid_bookings:
        booking_ids = [b.id for b in unpaid_bookings]
        for p_row in db.query(Payment).filter(Payment.booking_id.in_(booking_ids)).all():
            payments_by_booking.setdefault(p_row.booking_id, []).append(p_row)

    from app.services import mercadopago_service
    from app.services.booking_notifications import notify_admins

    for b in unpaid_bookings:
        payments = payments_by_booking.get(b.id, [])
        if any(p.status in (PaymentStatus.retained, PaymentStatus.released) for p in payments):
            continue

        ref_dt = (b.quoted_at or b.updated_at or b.created_at or now_utc).replace(tzinfo=None)
        age = now_utc - ref_dt
        b_id = str(b.id)
        contractor_user = b.contractor.user if b.contractor else None
        musician_user = b.musician.user if b.musician else None
        is_guest_contractor = bool(
            not contractor_user
            or not contractor_user.email
            or contractor_user.email.endswith("@guest.local")
        )

        if age >= timedelta(days=UNPAID_EXPIRY_DAYS):
            # P-02 / P-12: Bloquear la reserva y conciliar con Mercado Pago antes de cancelar.
            locked_b = db.get(Booking, b.id, with_for_update=True)
            if not locked_b or locked_b.status not in unpaid_statuses:
                continue
            b = locked_b

            if mercadopago_service.is_mercadopago_configured():
                try:
                    mercadopago_service.reconcile_booking_payments(db, b)
                    db.refresh(b)
                except Exception as exc:
                    logger.warning(
                        "Error conciliando pagos de Mercado Pago antes de expirar reserva %s: %s",
                        b_id,
                        exc,
                    )

            payments = db.query(Payment).filter(Payment.booking_id == b.id).all()
            has_paid_or_approved = (
                b.status not in unpaid_statuses
                or any(p.status in (PaymentStatus.retained, PaymentStatus.released) for p in payments)
                or any(
                    isinstance(p.gateway_metadata, dict) and p.gateway_metadata.get("status") == "approved"
                    for p in payments
                )
            )
            if has_paid_or_approved:
                stats["reconciled_unpaid"] = stats.get("reconciled_unpaid", 0) + 1
                try:
                    notify_admins(
                        db,
                        type="payment_alert",
                        title="Reserva con pago aprobado detectada antes de expirar",
                        message=(
                            f"La reserva #{b_id[:8]} tenía un pago aprobado en Mercado Pago "
                            f"y no fue cancelada por el cron de expiración."
                        ),
                        meta={"booking_id": b_id, "reason": "reconciled_before_expiry"},
                    )
                except Exception as exc:
                    logger.warning("No se pudo alertar a admins sobre reserva conciliada %s: %s", b_id, exc)
                continue

            for p in payments:
                if p.status == PaymentStatus.initiated:
                    p.status = PaymentStatus.failed
            b.status = BookingStatus.cancelled
            b.cancelled_by = "system"
            b.cancelled_at = now_utc
            b.rejection_reason = "Expirada por falta de pago en el plazo de 7 días"
            if contractor_user and not is_guest_contractor:
                notify_user(
                    db,
                    user=contractor_user,
                    type="booking_expired",
                    title="Reserva expirada por falta de pago",
                    message="La reserva expiró porque no se completó el pago dentro de los 7 días.",
                    booking_id=b_id,
                )
            if musician_user:
                notify_user(
                    db,
                    user=musician_user,
                    type="booking_expired",
                    title="Reserva expirada por falta de pago",
                    message="La propuesta expiró tras 7 días sin pago confirmado del cliente.",
                    booking_id=b_id,
                )
            stats["expired_unpaid"] += 1
        elif age >= timedelta(days=UNPAID_WARNING_DAYS):
            if (
                contractor_user
                and not is_guest_contractor
                and not _has_notification(db, contractor_user.id, "booking_expiration_warning", b_id)
            ):
                notify_user(
                    db,
                    user=contractor_user,
                    type="booking_expiration_warning",
                    title="Tu reserva vence en 24 horas",
                    message="Completa la firma y el pago en las próximas 24 horas para conservar tu fecha.",
                    booking_id=b_id,
                )
                stats["warned_unpaid"] += 1
            if musician_user and not _has_notification(
                db, musician_user.id, "booking_expiration_warning", b_id
            ):
                msg = (
                    "El cliente no tiene correo registrado. Contáctalo por WhatsApp o teléfono: la propuesta vence en 24 horas."
                    if is_guest_contractor
                    else "La propuesta enviada al cliente vencerá en menos de 24 horas si no se registra el pago."
                )
                notify_user(
                    db,
                    user=musician_user,
                    type="booking_expiration_warning",
                    title="Propuesta por vencer en 24 horas",
                    message=msg,
                    booking_id=b_id,
                )
                if is_guest_contractor:
                    stats["warned_unpaid"] += 1

    # 3. WP-04 (Paso 6): Expirar `change_pending` tras 48 h (o si ya empezó el evento) volviendo a `payment_retained`
    from app.services.booking_lifecycle import clear_pending_changes, is_event_started
    from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
    from app.services.booking_notifications import notify_admins

    change_pending_bookings = (
        db.query(Booking)
        .options(joinedload(Booking.contractor), joinedload(Booking.musician))
        .filter(Booking.status == BookingStatus.change_pending)
        .all()
    )
    for b in change_pending_bookings:
        ref_dt = (b.change_requested_at or b.updated_at or b.created_at or now_utc).replace(tzinfo=None)
        age = now_utc - ref_dt
        if age >= timedelta(hours=CHANGE_PENDING_EXPIRY_HOURS) or is_event_started(b):
            clear_pending_changes(b)
            b.status = BookingStatus.payment_retained
            stats["expired_change_pending"] = stats.get("expired_change_pending", 0) + 1
            b_id = str(b.id)
            contractor_user = b.contractor.user if b.contractor else None
            musician_user = b.musician.user if b.musician else None
            if contractor_user:
                notify_user(
                    db,
                    user=contractor_user,
                    type="booking_change_expired",
                    title="Solicitud de cambio expirada",
                    message="La solicitud de cambio expiró sin respuesta; la reserva continúa confirmada con los datos originales.",
                    booking_id=b_id,
                )
            if musician_user:
                notify_user(
                    db,
                    user=musician_user,
                    type="booking_change_expired",
                    title="Solicitud de cambio expirada",
                    message="La solicitud de cambio pendiente expiró; la reserva continúa confirmada con los datos originales.",
                    booking_id=b_id,
                )

    # 4. WP-04 (Paso 6): Recordatorios para reembolsos en `pending_approval` (> 24 h) y quejas abiertas sin respuesta (> 24 h)
    pending_refund_bookings = (
        db.query(Booking)
        .filter(
            Booking.status == BookingStatus.cancelled,
            Booking.cancellation_refund_status == "pending_approval",
        )
        .all()
    )
    for b in pending_refund_bookings:
        ref_dt = (b.cancelled_at or b.updated_at or b.created_at or now_utc).replace(tzinfo=None)
        if now_utc - ref_dt >= timedelta(hours=24):
            try:
                notify_admins(
                    db,
                    type="refund_approval_reminder",
                    title="Reembolso de cancelación pendiente de aprobación",
                    message=f"La reserva cancelada #{str(b.id)[:8]} lleva más de 24 h esperando aprobación de reembolso.",
                    meta={"booking_id": str(b.id)},
                )
                stats["refund_approval_reminders"] = stats.get("refund_approval_reminders", 0) + 1
            except Exception as exc:
                logger.warning("No se pudo enviar recordatorio de reembolso %s: %s", b.id, exc)

    open_complaints = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.status == BookingComplaintStatus.open)
        .all()
    )
    for comp in open_complaints:
        ref_dt = (comp.created_at or now_utc).replace(tzinfo=None)
        if now_utc - ref_dt >= timedelta(hours=24):
            b_obj = db.get(Booking, comp.booking_id)
            musician_user = b_obj.musician.user if b_obj and b_obj.musician else None
            b_id = str(comp.booking_id)
            if musician_user and not _has_notification(db, musician_user.id, "complaint_response_reminder", b_id):
                notify_user(
                    db,
                    user=musician_user,
                    type="complaint_response_reminder",
                    title="Queja pendiente de tu respuesta",
                    message="Tienes una queja abierta hace más de 24 h. Acepta o presenta tu descargo para continuar con la liquidación.",
                    booking_id=b_id,
                )
                stats["complaint_response_reminders"] = stats.get("complaint_response_reminders", 0) + 1

    # 5. WP-08 (S-05): Purgar pings de ubicación antiguos según retención configurada
    from app.models.booking_location import BookingLocationPing

    retention_days = max(int(getattr(settings, "LOCATION_PING_RETENTION_DAYS", 7) or 7), 1)
    ping_cutoff = now_utc - timedelta(days=retention_days)
    purged_pings = (
        db.query(BookingLocationPing)
        .filter(BookingLocationPing.created_at < ping_cutoff)
        .delete(synchronize_session=False)
    )
    stats["purged_location_pings"] = int(purged_pings or 0)

    db.commit()
    return stats


def send_reminder(db, user, booking, other_party_name: str, role: str) -> None:
    if not user.email or user.email.endswith("@guest.local"):
        return

    log = (
        db.query(EmailLog)
        .filter(
            EmailLog.template_slug == "booking_review_reminder",
            EmailLog.recipient == user.email,
            EmailLog.meta.op("->>")("booking_id") == str(booking.id),
        )
        .first()
    )

    if log:
        return

    dashboard_path = "/musician/bookings" if role == "musician" else "/contractor/bookings"
    action_url = f"{settings.FRONTEND_URL.rstrip('/')}{dashboard_path}/{booking.id}"

    logger.info("Enviando recordatorio de reseña para booking %s a %s", booking.id, user.email)
    send_templated_email(
        db,
        slug="booking_review_reminder",
        to=user.email,
        context={
            "user_name": user.fullname or user.username or "Usuario",
            "other_party_name": other_party_name,
            "action_url": action_url,
        },
        user_id=user.id,
        meta={"booking_id": str(booking.id), "reminder_type": "review"},
    )
    db.commit()


def main() -> None:
    db = SessionLocal()
    try:
        expire_stale_bookings(db)

        lima_tz = ZoneInfo("America/Lima")
        now_lima = datetime.now(lima_tz)

        valid_statuses = [
            BookingStatus.payment_retained,
            BookingStatus.in_progress,
            BookingStatus.payment_released,
            BookingStatus.completed,
        ]

        bookings = (
            db.query(Booking)
            .options(joinedload(Booking.contractor), joinedload(Booking.musician))
            .filter(Booking.status.in_(valid_statuses))
            .all()
        )

        for b in bookings:
            if not b.event_date or not b.start_time:
                continue

            event_dt_unaware = datetime.combine(b.event_date, b.start_time)
            event_dt_lima = event_dt_unaware.replace(tzinfo=lima_tz)

            time_since_start = now_lima - event_dt_lima
            if time_since_start < timedelta(hours=5):
                continue

            contractor_prof = b.contractor
            musician_prof = b.musician

            contractor_user = contractor_prof.user if contractor_prof else None
            musician_user = musician_prof.user if musician_prof else None

            reviews = (
                db.query(BookingReview)
                .filter(BookingReview.booking_id == b.id, BookingReview.is_final.is_(True))
                .all()
            )
            authors = {r.author_user_id for r in reviews}
            if musician_user and (
                db.query(ContractorRecommendation.id)
                .filter(ContractorRecommendation.booking_id == b.id)
                .first()
                is not None
            ):
                authors.add(musician_user.id)

            if (
                contractor_user
                and musician_user
                and (contractor_user.id in authors)
                and (musician_user.id in authors)
            ):
                continue

            if contractor_user and contractor_user.id not in authors:
                send_reminder(
                    db,
                    contractor_user,
                    b,
                    (musician_user.fullname or "el músico") if musician_user else "el músico",
                    "contractor",
                )

            if musician_user and musician_user.id not in authors:
                send_reminder(
                    db,
                    musician_user,
                    b,
                    (contractor_user.fullname or "el contratista")
                    if contractor_user
                    else "el contratista",
                    "musician",
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
