import hashlib
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.booking_location import BookingLocationPing
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    BookingMemberPayout,
    BookingMemberPayoutStatus,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.musician_profile import MusicianProfile, ProfileStatus
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _setup_users(db):
    admin = User(
        email="admin_rem@test.com",
        fullname="Admin Rem",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    musician_user = User(
        email="musician_rem@test.com",
        fullname="Mariachi Rem",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    contractor_user = User(
        email="contractor_rem@test.com",
        fullname="Contratista Rem",
        phone="999111222",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add_all([admin, musician_user, contractor_user])
    db.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi Rem",
        slug=f"mariachi-rem-{musician_user.id.hex[:6]}",
        status=ProfileStatus.published,
        signature_image_url="/uploads/private/firma-rem.png",
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        city="Ayacucho",
        document_type="DNI",
        document_number="44556677",
    )
    db.add_all([musician, contractor])
    db.commit()
    return admin, musician_user, musician, contractor_user, contractor


def test_bug_18_and_s16_admin_cancel_notifies_both_parties_and_invalid_uuid_returns_400(
    client, db_session
):
    """BUG-18 & S16: Admin cancel notifies contractor & musician; invalid UUIDs return 400."""
    admin, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(19, 0),
        end_time=time(21, 0),
        location_address="Jr. Libertad 100",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.accepted,
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin"))

    # S16: Invalid UUID path params return 400, not 500
    res_bad = client.post("/api/v1/admin/musicians/not-a-uuid/approve")
    assert res_bad.status_code == 400

    res_cancel = client.post(
        f"/api/v1/admin/bookings/{booking.id}/cancel",
        json={"reason": "Fuerza mayor", "refund_percent": 100},
    )
    assert res_cancel.status_code == 200, res_cancel.text

    c_notif = (
        db_session.query(Notification)
        .filter(
            Notification.user_id == contractor_user.id,
            Notification.type == "booking_cancelled",
        )
        .first()
    )
    m_notif = (
        db_session.query(Notification)
        .filter(
            Notification.user_id == musician_user.id,
            Notification.type == "booking_cancelled",
        )
        .first()
    )
    assert c_notif is not None
    assert m_notif is not None


def test_bug_20_retain_and_release_payment_state_guards(client, db_session):
    """BUG-20: Cannot retain payment on cancelled/completed booking, and release does not regress completed."""
    admin, _, musician, _, contractor = _setup_users(db_session)
    b_cancelled = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=5),
        start_time=time(18, 0),
        location_address="Av. Sol 1",
        event_type="Serenata",
        price_agreed=Decimal("400.00"),
        status=BookingStatus.cancelled,
    )
    b_completed = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() - timedelta(days=2),
        start_time=time(18, 0),
        location_address="Av. Sol 2",
        event_type="Serenata",
        price_agreed=Decimal("400.00"),
        status=BookingStatus.completed,
    )
    db_session.add_all([b_cancelled, b_completed])
    db_session.flush()

    p_cancelled = Payment(
        booking_id=b_cancelled.id,
        amount=Decimal("400.00"),
        payment_type="full",
        status=PaymentStatus.initiated,
    )
    p_completed = Payment(
        booking_id=b_completed.id,
        amount=Decimal("400.00"),
        payment_type="full",
        status=PaymentStatus.retained,
    )
    db_session.add_all([p_cancelled, p_completed])
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin"))

    res_retain = client.post(f"/api/v1/payments/{p_cancelled.id}/retain")
    assert res_retain.status_code == 400

    res_release = client.post(f"/api/v1/payments/{p_completed.id}/release")
    assert res_release.status_code == 200, res_release.text
    db_session.refresh(b_completed)
    assert b_completed.status == BookingStatus.completed


def test_bug_22_cancel_change_and_in_progress_cancellation(client, db_session):
    """BUG-22: Contractor can withdraw change_pending via /cancel-change, and in_progress can be cancelled."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=10),
        start_time=time(18, 0),
        location_address="Av. Original 100",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.change_pending,
        pending_location_address="Av. Nueva 200",
        change_requested_by="contractor",
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_withdraw = client.post(f"/api/v1/bookings/{booking.id}/cancel-change")
    assert res_withdraw.status_code == 200, res_withdraw.text
    db_session.refresh(booking)
    assert booking.status == BookingStatus.payment_retained
    assert booking.pending_location_address is None

    # Transition to in_progress and verify cancellation is allowed
    booking.status = BookingStatus.in_progress
    db_session.commit()
    res_cancel = client.post(f"/api/v1/bookings/{booking.id}/cancel")
    assert res_cancel.status_code == 200, res_cancel.text
    db_session.refresh(booking)
    assert booking.status == BookingStatus.cancelled


def test_bug_23_booking_update_rejects_explicit_null_required_fields(client, db_session):
    """BUG-23: PUT /bookings/{id} and POST /bookings/{id}/reopen-quote reject explicit null for required fields."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=8),
        start_time=time(19, 0),
        location_address="Av. Real 123",
        event_type="Serenata",
        status=BookingStatus.requested,
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_null = client.put(
        f"/api/v1/bookings/{booking.id}",
        json={"event_date": None},
    )
    assert res_null.status_code == 422


def test_bug_27_onboard_musician_requires_minimum_fields(client, db_session):
    """BUG-27: POST /profiles/musician/onboard rejects incomplete draft profile."""
    user = User(
        email="empty_draft@test.com",
        fullname="Empty Draft",
        role=UserRole.musician,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name="",
        slug=f"empty-{user.id.hex[:6]}",
        status=ProfileStatus.draft,
    )
    db_session.add(profile)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(user.id), role="musician"))
    res = client.post("/api/v1/profiles/musician/onboard")
    assert res.status_code == 400


def test_bug_29_notifications_pagination_and_read_all(client, db_session):
    """BUG-29: GET /notifications/me supports skip/limit and POST /notifications/read-all marks all read."""
    _, _, _, contractor_user, _ = _setup_users(db_session)
    for i in range(5):
        db_session.add(
            Notification(
                user_id=contractor_user.id,
                title=f"Notif {i}",
                message="Mensaje",
                type="info",
                is_read=False,
            )
        )
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_page = client.get("/api/v1/notifications/me?skip=0&limit=2")
    assert res_page.status_code == 200
    assert len(res_page.json()) == 2

    res_read_all = client.post("/api/v1/notifications/read-all")
    assert res_read_all.status_code == 200
    assert res_read_all.json()["updated"] == 5


def test_bug_32_and_bug_33_member_payout_guards_and_location_history_order(client, db_session):
    """BUG-32 & BUG-33: Member payout rejects >2 decimals or marking 0 amount paid; location history returns latest in asc order."""
    _, musician_user, musician, _, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date(),
        start_time=time(18, 0),
        location_address="Av. Sol 500",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.in_progress,
    )
    member = EnsembleMember(
        leader_user_id=musician_user.id,
        fullname="Trompetista Uno",
        email="trompeta1@test.com",
        specialties=["Trompeta"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add_all([booking, member])
    db_session.flush()

    rec = ContractorRecommendation(
        booking_id=booking.id,
        musician_id=musician.id,
        contractor_id=contractor.id,
        rating=5,
        comment="Excelente contratista",
    )
    zero_payout = BookingMemberPayout(
        booking_id=booking.id,
        ensemble_member_id=member.id,
        amount=Decimal("0.00"),
        currency="PEN",
        status=BookingMemberPayoutStatus.draft,
    )
    p1 = BookingLocationPing(
        booking_id=booking.id,
        user_id=musician_user.id,
        party="leader",
        action="update",
        lat=-13.16,
        lng=-74.22,
        created_at=datetime.utcnow() - timedelta(minutes=10),
    )
    p2 = BookingLocationPing(
        booking_id=booking.id,
        user_id=musician_user.id,
        party="leader",
        action="update",
        lat=-13.15,
        lng=-74.21,
        created_at=datetime.utcnow() - timedelta(minutes=1),
    )
    db_session.add_all([rec, zero_payout, p1, p2])
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(musician_user.id), role="musician")
    )

    # BUG-32: >2 decimals rejected with 422
    res_dec = client.put(
        f"/api/v1/bookings/{booking.id}/member-payouts",
        json={
            "items": [{"ensemble_member_id": str(member.id), "amount": "100.123"}],
            "lock": False,
        },
    )
    assert res_dec.status_code == 422

    # BUG-32: Cannot mark a 0.00 payout as paid
    res_zero_paid = client.post(
        f"/api/v1/bookings/{booking.id}/member-payouts/{zero_payout.id}/mark-paid"
    )
    assert res_zero_paid.status_code == 400

    # BUG-33: Location history returns chronological order (p1 then p2)
    res_hist = client.get(f"/api/v1/bookings/{booking.id}/live-location/history")
    assert res_hist.status_code == 200
    hist = res_hist.json()
    assert len(hist) == 2
    assert hist[0]["id"] == str(p1.id)
    assert hist[1]["id"] == str(p2.id)


def test_bug_35_get_booking_does_not_mutate_db(client, db_session):
    """BUG-35: GET /bookings/{id} and GET /bookings compute fee preview without persisting to Booking row."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=5),
        start_time=time(18, 0),
        location_address="Av. Sol 999",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        platform_fee_percent=None,
        platform_fee_amount=None,
        status=BookingStatus.accepted,
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res = client.get(f"/api/v1/bookings/{booking.id}")
    assert res.status_code == 200
    assert res.json()["platform_fee_percent"] is not None

    db_session.refresh(booking)
    assert booking.platform_fee_percent is None
    assert booking.platform_fee_amount is None


def test_bug2_02_booking_invite_respond_accept(client, db_session):
    """BUG2-02: POST /invites/member/{token}/respond works without TypeError."""
    _, musician_user, musician, _, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=5),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.payment_retained,
    )
    member = EnsembleMember(
        leader_user_id=musician_user.id,
        fullname="Violinista Invitado",
        email="violin@test.com",
        specialties=["Violín"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add_all([booking, member])
    db_session.flush()

    raw_token = "inv-token-bug2-02"
    invite = BookingMemberInvite(
        booking_id=booking.id,
        ensemble_member_id=member.id,
        status=BookingMemberInviteStatus.pending,
        response_token=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
    )
    db_session.add(invite)
    db_session.commit()

    res = client.post(
        f"/api/v1/invites/member/{raw_token}/respond",
        json={"action": "accept"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "accepted"


def test_bug2_03_booking_link_enforces_preset_fields_and_blocks_hijack(client, db_session):
    """BUG2-03: checkout_booking_link enforces link's preset email/date/time and prevents cross-contractor hijack."""
    from app.models.booking_link import BookingLink, BookingLinkStatus

    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    other_user = User(
        email="other_c@test.com",
        fullname="Otro Contratista",
        phone="999333444",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(other_user)
    db_session.flush()
    other_contractor = ContractorProfile(
        user_id=other_user.id,
        city="Ayacucho",
        document_type="DNI",
        document_number="87654321",
    )
    db_session.add(other_contractor)
    db_session.flush()

    target_date = now_peru().date() + timedelta(days=7)
    link = BookingLink(
        token="link-preset-bug2-03",
        musician_id=musician.id,
        price=Decimal("700.00"),
        event_date=target_date,
        start_time=time(19, 0),
        client_email=contractor_user.email,
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(link)
    db_session.commit()

    # 1. Wrong email must be rejected with 403
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(other_user.id), role="contractor")
    )
    res_wrong_email = client.post(
        "/api/v1/booking-links/link-preset-bug2-03/checkout",
        json={
            "event_date": str(target_date),
            "start_time": "19:00:00",
            "event_type": "Serenata",
            "location_address": "Av. Sol 100",
            "location_city": "Ayacucho",
            "email": other_user.email,
            "fullname": "Otro",
            "phone": "999333444",
            "document_number": "87654321",
        },
    )
    assert res_wrong_email.status_code == 403

    # 2. Wrong date with right contractor must be rejected with 400
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_wrong_date = client.post(
        "/api/v1/booking-links/link-preset-bug2-03/checkout",
        json={
            "event_date": str(target_date + timedelta(days=1)),
            "start_time": "19:00:00",
            "event_type": "Serenata",
            "location_address": "Av. Sol 100",
            "location_city": "Ayacucho",
            "email": contractor_user.email,
            "fullname": contractor_user.fullname,
            "phone": contractor_user.phone,
            "document_number": "44556677",
        },
    )
    assert res_wrong_date.status_code == 400

    # 3. Open link (no client_email) already checked out by contractor_user cannot be hijacked by other_user
    open_link = BookingLink(
        token="link-open-bug2-03",
        musician_id=musician.id,
        price=Decimal("700.00"),
        event_date=target_date,
        start_time=time(21, 0),
        client_email=None,
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(open_link)
    db_session.commit()

    res_first = client.post(
        "/api/v1/booking-links/link-open-bug2-03/checkout",
        json={
            "event_date": str(target_date),
            "start_time": "21:00:00",
            "event_type": "Serenata",
            "location_address": "Av. Sol 100",
            "location_city": "Ayacucho",
            "email": contractor_user.email,
            "fullname": contractor_user.fullname,
            "phone": contractor_user.phone,
            "document_number": "44556677",
        },
    )
    assert res_first.status_code == 200, res_first.text

    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(other_user.id), role="contractor")
    )
    res_hijack = client.post(
        "/api/v1/booking-links/link-open-bug2-03/checkout",
        json={
            "event_date": str(target_date),
            "start_time": "21:00:00",
            "event_type": "Serenata",
            "location_address": "Av. Sol 200",
            "location_city": "Ayacucho",
            "email": other_user.email,
            "fullname": other_user.fullname,
            "phone": other_user.phone,
            "document_number": "87654321",
        },
    )
    assert res_hijack.status_code == 409


def test_bug2_04_and_bug2_07_admin_payout_process_and_zero_percent_fee(client, db_session):
    """BUG2-04 & BUG2-07: Admin can process payout request with JSON body; 0% commission still charges MP fee."""
    from app.models.payout_request import PayoutRequest
    from app.services.platform_payment import compute_platform_fee_amount, contractor_payable_total

    admin, musician_user, musician, _, _ = _setup_users(db_session)
    req = PayoutRequest(
        musician_id=musician.id,
        amount=Decimal("300.00"),
        status="pending",
    )
    db_session.add(req)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin"))
    res = client.post(
        f"/api/v1/admin/payout-requests/{req.id}/process",
        json={"notes": "Transferencia CCI realizada"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "processed"

    # BUG2-07: 0% commission still includes MP pass-through fee
    fee_zero = compute_platform_fee_amount(Decimal("1000.00"), Decimal("0"))
    assert fee_zero > Decimal("0")
    dummy_booking = Booking(
        price_agreed=Decimal("1000.00"),
        platform_fee_percent=Decimal("0"),
        platform_fee_amount=fee_zero,
    )
    assert Decimal("1000.00") + fee_zero == contractor_payable_total(dummy_booking)


def test_bug2_08_final_review_and_complete_require_event_finished(client, db_session):
    """BUG2-08: Cannot submit final-review or complete while event is still ongoing (before end_time)."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    now = now_peru()
    # Event started 1 hour ago, ends 2 hours from now (if before 21:59)
    if now.hour <= 21:
        ev_date = now.date()
        st = time(max(0, now.hour - 1), 0)
        et = time(min(23, now.hour + 2), 59)
    else:
        ev_date = now.date() + timedelta(days=1)
        st = time(18, 0)
        et = time(20, 0)

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=ev_date,
        start_time=st,
        end_time=et,
        location_address="Av. Evento 123",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.in_progress,
    )
    db_session.add(booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_final = client.post(
        f"/api/v1/bookings/{booking.id}/final-review",
        json={"rating": 5, "comment": "Excelente presentación musical"},
    )
    assert res_final.status_code == 400

    res_complete = client.post(f"/api/v1/bookings/{booking.id}/complete")
    assert res_complete.status_code == 400


def test_bug2_09_and_bug2_12_onboard_4_fields_and_availability_statuses(client, db_session):
    """BUG2-09 & BUG2-12: Onboard requires only 4 wizard fields; requested/accepted do not block availability."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)

    # BUG2-09: Musician with only 4 wizard fields can onboard to pending_review
    u_wiz = User(
        email="wizard_mus@test.com",
        fullname="Wizard Mus",
        role=UserRole.musician,
        is_active=True,
    )
    db_session.add(u_wiz)
    db_session.flush()
    p_wiz = MusicianProfile(
        user_id=u_wiz.id,
        stage_name="Mariachi Wizard",
        slug="mariachi-wizard",
        genres=["Mariachi"],
        location_city="Ayacucho",
        profile_image_url="/uploads/avatar.jpg",
        status=ProfileStatus.draft,
    )
    db_session.add(p_wiz)
    db_session.commit()

    client.cookies.set("access_token", create_access_token(subject=str(u_wiz.id), role="musician"))
    res_onboard = client.post("/api/v1/profiles/musician/onboard")
    assert res_onboard.status_code == 200, res_onboard.text
    # FE3-04 / BUG4-07: incomplete 4-field profile stays in draft until full completeness is met
    assert res_onboard.json()["status"] == "draft"

    # BUG2-12: An accepted (unpaid/unsigned) booking does not block another contractor from requesting same slot
    future_date = now_peru().date() + timedelta(days=9)
    unpaid_booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Jr. Sol 111",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.accepted,
    )
    db_session.add(unpaid_booking)
    db_session.commit()

    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_req = client.post(
        "/api/v1/bookings",
        json={
            "musician_id": str(musician.id),
            "event_date": str(future_date),
            "start_time": "18:30:00",
            "end_time": "19:30:00",
            "location_address": "Jr. Sol 222",
            "location_city": "Ayacucho",
            "event_type": "Cumpleaños",
        },
    )
    assert res_req.status_code == 201, res_req.text


def test_bug2_14_and_bug2_15_and_bug_37(client, db_session):
    """BUG2-14 (NULL payment_type), BUG2-15 (end_time <= start_time), BUG-37 (duplicate musician fullname)."""
    from app.services.settlement import retained_total_for_booking

    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=6),
        start_time=time(18, 0),
        end_time=time(20, 0),
        location_address="Av. Sol 10",
        event_type="Serenata",
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("0"),
        platform_fee_amount=Decimal("0"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.flush()
    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=Decimal("500.00"),
            payment_type=None,
            status=PaymentStatus.retained,
        )
    )
    db_session.commit()

    assert retained_total_for_booking(db_session, booking.id) == 500.0

    # BUG2-15: end_time <= start_time rejected with 422
    client.cookies.set(
        "access_token", create_access_token(subject=str(contractor_user.id), role="contractor")
    )
    res_bad_time = client.post(
        "/api/v1/bookings",
        json={
            "musician_id": str(musician.id),
            "event_date": str(now_peru().date() + timedelta(days=6)),
            "start_time": "20:00:00",
            "end_time": "18:00:00",
            "location_address": "Av. Sol 10",
            "event_type": "Serenata",
        },
    )
    assert res_bad_time.status_code == 422

    # BUG-37: Two musicians with the same real fullname can register
    client.cookies.clear()
    r1 = client.post(
        "/api/v1/auth/register",
        json={
            "email": "juan1_homonimo@test.com",
            "password": "Password123!",
            "fullname": "Juan Pérez Homónimo",
            "role": "musician",
            "accepted_terms": True,
        },
    )
    r2 = client.post(
        "/api/v1/auth/register",
        json={
            "email": "juan2_homonimo@test.com",
            "password": "Password123!",
            "fullname": "Juan Pérez Homónimo",
            "role": "musician",
            "accepted_terms": True,
        },
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
