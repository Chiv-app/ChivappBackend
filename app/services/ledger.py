from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.ledger_entry import LedgerEntry

logger = logging.getLogger(__name__)


def record_ledger_entry(
    db: Session,
    *,
    entry_type: str,
    amount: Decimal | float | int | str,
    booking_id: UUID | str | None = None,
    payment_id: UUID | str | None = None,
    actor_user_id: UUID | str | None = None,
    currency: str = "PEN",
    description: str | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry | None:
    """Registra un asiento inmutable en `ledger_entry` (WP-14)."""
    try:
        dec_amount = Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        entry = LedgerEntry(
            booking_id=UUID(str(booking_id)) if booking_id else None,
            payment_id=UUID(str(payment_id)) if payment_id else None,
            actor_user_id=UUID(str(actor_user_id)) if actor_user_id else None,
            entry_type=entry_type,
            amount=dec_amount,
            currency=currency or "PEN",
            description=description,
            meta=meta,
        )
        # Savepoint: si el asiento falla no deja la sesión en estado inválido ni revierte el resto.
        with db.begin_nested():
            db.add(entry)
            db.flush()
        return entry
    except Exception as exc:
        logger.error("No se pudo registrar asiento en ledger_entry (%s): %s", entry_type, exc)
        return None
