from decimal import Decimal

from app.models.booking import Booking
from app.services.platform_payment import (
    compute_platform_fee_amount,
    contractor_advance_due,
    contractor_payable_total,
    contractor_remaining_after_advance,
    musician_portion_of_paid,
)


def _booking(price_agreed=None, platform_fee_amount=None, advance_amount=None, platform_fee_percent=None) -> Booking:
    booking = Booking()
    booking.price_agreed = price_agreed
    booking.platform_fee_amount = platform_fee_amount
    booking.advance_amount = advance_amount
    booking.platform_fee_percent = platform_fee_percent
    return booking


def test_compute_platform_fee_amount_rounds_half_up():
    assert compute_platform_fee_amount(Decimal("500"), Decimal("2")) == Decimal("10.00")


def test_compute_platform_fee_amount_zero_when_price_or_percent_not_positive():
    assert compute_platform_fee_amount(Decimal("0"), Decimal("2")) == Decimal("0.00")
    assert compute_platform_fee_amount(Decimal("500"), Decimal("0")) == Decimal("0.00")


def test_contractor_payable_total_adds_fee_to_price():
    # M=500, g=0.02, F=1.18, p=0.0412 -> C_unico = (500*1.02 + 1.18) / 0.9588 = 533.15
    booking = _booking(price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), platform_fee_percent=Decimal("2"))
    assert contractor_payable_total(booking) == Decimal("533.15")


def test_contractor_payable_total_none_without_price():
    booking = _booking(price_agreed=None)
    assert contractor_payable_total(booking) is None


def test_contractor_advance_due_defaults_to_full_total_without_advance():
    booking = _booking(price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), platform_fee_percent=Decimal("2"))
    assert contractor_advance_due(booking) == Decimal("533.15")


def test_contractor_advance_due_uses_advance_amount_when_set():
    booking = _booking(
        price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), advance_amount=Decimal("200"), platform_fee_percent=Decimal("2")
    )
    # It now always returns the full payable total
    assert contractor_advance_due(booking) == Decimal("533.15")


def test_contractor_remaining_after_advance():
    booking = _booking(
        price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), advance_amount=Decimal("200")
    )
    # With single payment, there's never a remaining balance
    assert contractor_remaining_after_advance(booking) == Decimal("0.00")


def test_contractor_remaining_after_advance_never_negative():
    booking = _booking(
        price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), advance_amount=Decimal("999")
    )
    assert contractor_remaining_after_advance(booking) == Decimal("0.00")


def test_musician_portion_of_paid_splits_out_platform_fee():
    booking = _booking(price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), platform_fee_percent=Decimal("2"))
    # paid_total pays the full payable amount (533.15) -> musician gets exactly the price (500)
    assert musician_portion_of_paid(booking, Decimal("533.15")) == Decimal("500.00")


def test_musician_portion_of_paid_zero_when_nothing_paid():
    booking = _booking(price_agreed=Decimal("500"), platform_fee_amount=Decimal("10"), platform_fee_percent=Decimal("2"))
    assert musician_portion_of_paid(booking, Decimal("0")) == Decimal("0.00")


def test_musician_portion_of_paid_all_to_musician_when_no_fee():
    # M=500, g=0, F=1.18, p=0.0412 -> C_unico = (500 + 1.18) / 0.9588 = 522.72
    booking = _booking(price_agreed=Decimal("500"), platform_fee_amount=None, platform_fee_percent=Decimal("0"))
    assert musician_portion_of_paid(booking, Decimal("522.72")) == Decimal("500.00")


def test_expected_payment_amount_is_server_side_single_payment():
    from app.services.mercadopago_service import expected_payment_amount

    booking = _booking(price_agreed=Decimal("500"), platform_fee_percent=Decimal("2"))
    total = contractor_payable_total(booking)
    assert expected_payment_amount(None, booking, "full") == total
    assert expected_payment_amount(None, booking, "advance") == total
