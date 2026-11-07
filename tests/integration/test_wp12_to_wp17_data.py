"""Integration tests for Fase 3 Data packages (WP-12, WP-13, WP-14, WP-15, WP-16, WP-17)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.ledger_entry import LedgerEntry
from app.models.musician_media import MediaType, MusicianMedia
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.booking_state_machine import compute_allowed_actions, compute_visible_stage
from app.services.platform_payment import (
    get_cached_platform_fee_percent,
    invalidate_platform_payment_cache,
)
from app.services.settlement import release_retained_payments


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def test_wp12_musician_search_batch_and_fee_cache(client: TestClient, db_session: Session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"search_mus_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="Músico Search",
        is_verified=True,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=f"Banda Batch {suffix}",
        slug=f"banda-batch-{suffix}",
        status=ProfileStatus.published,
        location_city="Lima",
        genres=["Rock", "Pop"],
        instruments=["Guitarra"],
        price_per_event=Decimal("1200.00"),
    )
    db_session.add(profile)
    db_session.flush()
    db_session.add(
        MusicianMedia(
            musician_id=profile.id,
            type=MediaType.image,
            url="/uploads/cover-batch.jpg",
            order_index=0,
        )
    )
    db_session.commit()

    res = client.post(
        "/api/v1/musicians/search",
        json={"city": "Lima", "genres": ["Rock"], "limit": 20},
    )
    assert res.status_code == 200
    items = res.json()
    matched = [i for i in items if i["musician_id"] == str(profile.id)]
    assert len(matched) == 1
    assert matched[0]["main_image_url"] == "/uploads/cover-batch.jpg"

    invalidate_platform_payment_cache()
    pct = get_cached_platform_fee_percent(db_session)
    assert pct >= Decimal("0")


def test_wp14_ledger_entries_on_settlement_and_wp15_booking_stage_actions(
    client: TestClient,
    db_session: Session,
):
    suffix = uuid.uuid4().hex[:8]
    mus_user = User(
        email=f"mus_ledger_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="Músico Ledger",
        is_verified=True,
        is_active=True,
    )
    con_user = User(
        email=f"con_ledger_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.contractor,
        fullname="Cliente Ledger",
        is_verified=True,
        is_active=True,
    )
    db_session.add_all([mus_user, con_user])
    db_session.flush()

    mus = MusicianProfile(
        user_id=mus_user.id,
        stage_name=f"Orquesta Ledger {suffix}",
        slug=f"orquesta-ledger-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
    )
    con = ContractorProfile(
        user_id=con_user.id,
        status=ProfileStatus.published,
    )
    db_session.add_all([mus, con])
    db_session.flush()

    booking = Booking(
        musician_id=mus.id,
        contractor_id=con.id,
        event_type="Aniversario",
        event_date=date.today() + timedelta(days=15),
        start_time=time(20, 0),
        end_time=time(22, 0),
        location_address="Av. Central 500",
        location_city="Lima",
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("2.00"),
        status=BookingStatus.payment_retained,
    )
    from app.services.platform_payment import sync_booking_platform_fee

    sync_booking_platform_fee(db_session, booking, use_current_settings=False)
    db_session.add(booking)
    db_session.flush()

    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("1000.00") + Decimal(str(booking.platform_fee_amount or 0)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        gateway_provider="mercadopago",
        gateway_payment_id=f"mp-{suffix}",
    )
    db_session.add(payment)
    db_session.commit()

    # WP-15: GET /bookings/{id} exposes visible_stage and allowed_actions
    b_res = client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=_auth_headers(con_user),
    )
    assert b_res.status_code == 200
    b_json = b_res.json()
    assert b_json["visible_stage"] == "confirmada"
    assert "CANCEL" in b_json["allowed_actions"]
    assert "CHAT_MESSAGE" in b_json["allowed_actions"]
    assert compute_visible_stage(BookingStatus.requested) == "solicitud"
    assert compute_visible_stage(BookingStatus.accepted) == "cotizacion"
    assert compute_visible_stage(BookingStatus.completed) == "evento_y_cierre"
    assert compute_allowed_actions(booking, viewer_role="member") == ["CHAT_MESSAGE"]

    # WP-14: Releasing retained payment writes immutable LedgerEntry rows
    release_retained_payments(db_session, booking.id, payout_reference="OP-LEDGER-1")
    db_session.commit()

    entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.booking_id == booking.id)
        .order_by(LedgerEntry.created_at.asc())
        .all()
    )
    entry_types = {e.entry_type for e in entries}
    assert "musician_payout" in entry_types
    assert "platform_fee" in entry_types
    payout_entry = next(e for e in entries if e.entry_type == "musician_payout")
    assert Decimal(str(payout_entry.amount)) == Decimal("1000.00")
