from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.platform_payment import PlatformPaymentSettings


def get_or_create_platform_payment_settings(db: Session) -> PlatformPaymentSettings:
    row = db.query(PlatformPaymentSettings).first()
    if row:
        if row.phone_number is None:
            row.phone_number = ""
        if row.phone_label is None:
            row.phone_label = "Yape / Plin"
        if row.platform_fee_percent is None:
            row.platform_fee_percent = Decimal("2")
        return row
    row = PlatformPaymentSettings(
        phone_number="",
        phone_label="Yape / Plin",
        account_name="Chivapp",
        instructions="Realiza el pago a la cuenta de la plataforma y sube el comprobante.",
        platform_fee_percent=Decimal("2"),
    )
    db.add(row)
    db.flush()
    return row


def compute_platform_fee_amount(price: Decimal, percent: Decimal) -> Decimal:
    if price <= 0 or percent <= 0:
        return Decimal("0.00")
    return (price * percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def sync_booking_platform_fee(
    db: Session | None,
    booking: Booking,
    *,
    use_current_settings: bool = True,
) -> None:
    """Keep booking fee snapshot in sync with price_agreed.

    When use_current_settings is True, refresh percent from admin settings
    (new quotes / price writes). Otherwise keep the stored percent and only
    recompute the absolute amount (e.g. after accepting a pending price change).
    """
    if use_current_settings or booking.platform_fee_percent is None:
        if db is None:
            if booking.platform_fee_percent is None:
                booking.platform_fee_percent = Decimal("0")
        else:
            settings = get_or_create_platform_payment_settings(db)
            booking.platform_fee_percent = Decimal(
                str(settings.platform_fee_percent or 0)
            )

    percent = Decimal(str(booking.platform_fee_percent or 0))
    if booking.price_agreed is None:
        booking.platform_fee_amount = None
        return

    price = Decimal(str(booking.price_agreed))
    booking.platform_fee_amount = compute_platform_fee_amount(price, percent)


def contractor_payable_total(booking: Booking) -> Decimal | None:
    if booking.price_agreed is None:
        return None
    M = Decimal(str(booking.price_agreed))
    g_percent = Decimal(str(booking.platform_fee_percent or 0))
    g = g_percent / Decimal("100")
    
    # Mercado Pago variables (Peru)
    F = Decimal("1.18")
    p = Decimal("0.0412")
    
    c_unico = (M * (Decimal("1") + g) + F) / (Decimal("1") - p)
    return c_unico.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def musician_portion_of_paid(booking: Booking, paid_total: Decimal) -> Decimal:
    """Share of contractor payments that belongs to the musician."""
    price = Decimal(str(booking.price_agreed or 0))
    total_payable = contractor_payable_total(booking)
    
    if not total_payable or total_payable <= 0:
        return paid_total
        
    return (paid_total * price / total_payable).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
