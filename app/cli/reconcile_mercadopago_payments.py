"""Periodic reconciliation job for Mercado Pago payments (WP-01 / P-02)."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

from sqlalchemy import exists, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.services import mercadopago_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CANDIDATE_STATUSES = (
    BookingStatus.accepted,
    BookingStatus.contract_pending,
    BookingStatus.contract_signed,
    BookingStatus.payment_pending,
)


def run_reconciliation_batch(
    db: Session,
    *,
    now: datetime | None = None,
    batch_size: int = 50,
    force: bool = False,
    check_orphans: bool = False,
    auto_refund: bool | None = None,
) -> dict[str, int]:
    """
    Concilia reservas pendientes de pago con Mercado Pago por lotes.
    Usa `FOR UPDATE SKIP LOCKED` por reserva para evitar choques entre ejecuciones concurrentes.
    """
    stats = {
        "skipped": 0,
        "checked_bookings": 0,
        "reconciled_payments": 0,
        "auto_refunded": 0,
        "errors": 0,
        "orphans_detected": 0,
    }
    if not force and not getattr(settings, "MP_RECONCILIATION_ENABLED", False):
        logger.info("MP_RECONCILIATION_ENABLED está desactivado; omitiendo conciliación.")
        stats["skipped"] = 1
        return stats

    if not mercadopago_service.is_mercadopago_configured():
        logger.info("Mercado Pago no está configurado; omitiendo conciliación.")
        stats["skipped"] = 1
        return stats

    now_utc = (now or datetime.utcnow()).replace(tzinfo=None)
    seven_days_ago = now_utc - timedelta(days=7)
    thirty_min_ago = now_utc - timedelta(minutes=30)

    has_initiated_payment = exists().where(
        (Payment.booking_id == Booking.id) & (Payment.status == PaymentStatus.initiated)
    )
    has_settled_payment = exists().where(
        (Payment.booking_id == Booking.id)
        & Payment.status.in_((PaymentStatus.retained, PaymentStatus.released))
        & Payment.payment_type.is_distinct_from("refund")
    )

    candidate_ids = [
        row[0]
        for row in (
            db.query(Booking.id)
            .filter(
                Booking.status.in_(CANDIDATE_STATUSES),
                ~has_settled_payment,
                or_(
                    has_initiated_payment,
                    (Booking.created_at >= seven_days_ago) & (Booking.created_at <= thirty_min_ago),
                ),
            )
            .order_by(Booking.created_at.asc())
            .limit(batch_size)
            .all()
        )
    ]

    for b_id in candidate_ids:
        try:
            booking = (
                db.query(Booking)
                .filter(Booking.id == b_id, Booking.status.in_(CANDIDATE_STATUSES))
                .with_for_update(skip_locked=True)
                .first()
            )
            if booking is None:
                continue
            stats["checked_bookings"] += 1
            processed = mercadopago_service.reconcile_booking_payments(db, booking)
            stats["reconciled_payments"] += len(processed)
            db.commit()
        except Exception as exc:
            db.rollback()
            stats["errors"] += 1
            logger.error("Error conciliando reserva %s con Mercado Pago: %s", b_id, exc)

    should_auto_refund = (
        auto_refund
        if auto_refund is not None
        else bool(getattr(settings, "MP_AUTO_REFUND_NEEDS_REFUND", False))
    )
    if should_auto_refund:
        try:
            ref_stats = mercadopago_service.auto_refund_flagged_payments(db, batch_size=batch_size)
            stats["auto_refunded"] = ref_stats.get("refunded", 0)
        except Exception as exc:
            db.rollback()
            stats["errors"] += 1
            logger.error("Error ejecutando auto-reembolso de pagos needs_refund: %s", exc)

    if check_orphans:
        try:
            orphans = mercadopago_service.detect_orphan_mercadopago_payments(db)
            stats["orphans_detected"] = len(orphans)
        except Exception as exc:
            db.rollback()
            stats["errors"] += 1
            logger.error("Error buscando pagos huérfanos en Mercado Pago: %s", exc)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Concilia pagos de Mercado Pago con reservas locales.")
    parser.add_argument("--force", action="store_true", help="Ejecutar aunque MP_RECONCILIATION_ENABLED sea False")
    parser.add_argument("--check-orphans", action="store_true", help="Buscar pagos aprobados huérfanos de las últimas 24h")
    parser.add_argument("--batch-size", type=int, default=50, help="Tamaño de lote de reservas a conciliar")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = run_reconciliation_batch(
            db,
            batch_size=args.batch_size,
            force=args.force,
            check_orphans=args.check_orphans,
        )
        logger.info("Resultado de conciliación MP: %s", stats)
    finally:
        db.close()


if __name__ == "__main__":
    main()
