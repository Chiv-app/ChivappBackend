from datetime import date, time
from types import SimpleNamespace
from uuid import uuid4

from app.schemas.booking_link import BookingLinkCheckout
from app.services.booking_location import get_or_create_share, parse_event_coords


def test_booking_link_checkout_schema_accepts_location_reference():
    payload = BookingLinkCheckout(
        event_date=date(2027, 6, 15),
        start_time=time(20, 0),
        event_type="Cumpleaños",
        location_address="Av. José Larco 812, Miraflores",
        location_city="Miraflores",
        location_reference="-12.121987,-77.029876",
        fullname="Ana Gómez",
        email="ana@example.com",
        phone="987654321",
        accept_terms=True,
    )
    assert payload.location_reference == "-12.121987,-77.029876"


def test_parse_event_coords_and_get_or_create_share_syncs_updated_location():
    booking = SimpleNamespace(
        id=uuid4(),
        location_reference="-12.046400,-77.042800",
        location_address="Plaza Mayor de Lima",
        location_city="Lima",
    )
    assert parse_event_coords(booking) == (-12.0464, -77.0428)

    existing_share = SimpleNamespace(
        booking_id=booking.id,
        event_lat=-12.0464,
        event_lng=-77.0428,
        event_address="Plaza Mayor de Lima",
        event_city="Lima",
    )

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return existing_share

    class FakeSession:
        def __init__(self):
            self.flushed = False

        def query(self, _model):
            return FakeQuery()

        def flush(self):
            self.flushed = True

    db = FakeSession()

    # Update booking coordinates and address (e.g. after editing commitment)
    booking.location_reference = "-12.121987,-77.029876"
    booking.location_address = "Av. Larco 812, Miraflores"
    booking.location_city = "Miraflores"

    updated = get_or_create_share(db, booking)
    assert updated.event_lat == -12.121987
    assert updated.event_lng == -77.029876
    assert updated.event_address == "Av. Larco 812, Miraflores"
    assert updated.event_city == "Miraflores"
    assert db.flushed is True
