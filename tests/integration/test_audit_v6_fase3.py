"""Regression tests for Audit v6 — Fase 3 (Reservas y operación)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from app.cli.cron_review_reminders import expire_stale_bookings
from app.cli.migrate_private_documents import migrate_url
from app.core.config import settings
from app.core.hashing import hash_password as get_password_hash
from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.db import migrate as migrate_mod
from app.models.booking import Booking, BookingStatus
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import (
    BookingMemberPayout,
    BookingMemberPayoutStatus,
    EnsembleMember,
)
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services import uploads as uploads_mod
from app.services.calendar_service import decrypt_refresh_token, encrypt_refresh_token
from app.services.cancellation_policy import quote_cancellation
from app.services.contract_pdf import (
    apply_cancellation_policy_to_contract_html,
    build_default_contract_template_body,
)
from app.services.musician_booking import resolve_or_create_contractor_client


def _auth_header(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def _create_verified_musician(
    db,
    *,
    email: str,
    stage_name: str | None = None,
    created_at: datetime | None = None,
) -> tuple[User, MusicianProfile]:
    stage_name = stage_name or f"Mariachi Sol {email.split('@')[0]}"
    sig_url = "/uploads/private/fase3_sig.png"
    uploads_mod.ensure_private_upload_dir()
    (uploads_mod.PRIVATE_UPLOAD_DIR / "fase3_sig.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )
    user = User(
        email=email,
        fullname=stage_name,
        phone=f"9{abs(hash(email)) % 100000000:08d}",
        role=UserRole.musician,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()

    mp = MusicianProfile(
        user_id=user.id,
        stage_name=stage_name,
        slug=f"slug-{abs(hash(email)) % 1000000}",
        bio="Bio completa del grupo musical",
        portfolio_description="Descripción completa del portafolio",
        instruments=["Trompeta", "Violín"],
        genres=["Mariachi"],
        price_per_hour=Decimal("350.00"),
        location_city="Lima",
        location_zone="Miraflores",
        availability_type=AvailabilityType.both,
        profile_image_url="/uploads/profile.jpg",
        id_document_url="/uploads/private/dni.pdf",
        signature_image_url=sig_url,
        contract_template_title="Contrato de Presentación",
        contract_template_body="<p>El cliente podrá cancelar hasta 15 días calendario antes del evento con reembolso del 100%, o entre 7 y 14 días antes con reembolso del 50%.</p>",
        status=ProfileStatus.published,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(mp)
    db.commit()
    db.refresh(mp)
    return user, mp


def test_sec4_17_legacy_repertoire_url_without_scheme_does_not_500(client, db_session):
    _, mp = _create_verified_musician(
        db_session,
        email="sec417@example.com",
        stage_name="Mariachi Repertorio Legado",
    )
    # Bypass schema validator to simulate legacy dirty data in JSONB
    mp.repertoire = [
        {"title": "El Rey", "youtube_url": "youtube.com/watch?v=dQw4w9WgXcQ"},
        {"title": "Cielito Lindo", "youtube_url": "not-a-valid-url-at-all"},
    ]
    mp.showreel_video_url = "youtube.com/watch?v=dQw4w9WgXcQ"
    db_session.commit()

    list_res = client.get("/api/v1/profiles/musicians")
    assert list_res.status_code == 200, list_res.text
    items = list_res.json()
    found = next((x for x in items if x["id"] == str(mp.id)), None)
    assert found is not None
    assert found["showreel_video_url"] == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert found["repertoire"][0]["youtube_url"] == "https://youtube.com/watch?v=dQw4w9WgXcQ"
    assert found["repertoire"][1]["youtube_url"] is None

    detail_res = client.get(f"/api/v1/profiles/musicians/{mp.id}")
    assert detail_res.status_code == 200, detail_res.text


def test_bug6_07_musicians_pagination_order_and_total_header(client, db_session):
    base_time = datetime(2026, 1, 1, 12, 0, 0)
    created_ids = []
    for idx in range(5):
        _, mp = _create_verified_musician(
            db_session,
            email=f"page{idx}@example.com",
            stage_name=f"Mariachi Pag {idx}",
            created_at=base_time + timedelta(minutes=idx),
        )
        created_ids.append(str(mp.id))

    res_p1 = client.get("/api/v1/profiles/musicians?skip=0&limit=2")
    res_p2 = client.get("/api/v1/profiles/musicians?skip=2&limit=2")
    assert res_p1.status_code == 200
    assert res_p2.status_code == 200
    assert res_p1.headers.get("X-Total-Count") == "5"

    ids_p1 = [item["id"] for item in res_p1.json()]
    ids_p2 = [item["id"] for item in res_p2.json()]
    assert len(ids_p1) == 2
    assert len(ids_p2) == 2
    assert set(ids_p1).isdisjoint(set(ids_p2))
    # Newest first (idx 4, idx 3, idx 2, idx 1)
    assert ids_p1 == [created_ids[4], created_ids[3]]
    assert ids_p2 == [created_ids[2], created_ids[1]]


def test_bug5_10_contract_pdf_and_frozen_cancellation_policy(db_session, monkeypatch):
    monkeypatch.setattr(settings, "CANCEL_FULL_REFUND_MIN_DAYS", 20)
    monkeypatch.setattr(settings, "CANCEL_PARTIAL_REFUND_MIN_DAYS", 10)
    monkeypatch.setattr(settings, "CANCEL_PARTIAL_REFUND_PERCENT", 40)

    default_html = build_default_contract_template_body()
    assert "20 días calendario" in default_html
    assert "40 %" in default_html

    legacy_html = (
        "<p>Las cancelaciones y reembolsos se rigen por la política de Chivapp: si EL CLIENTE cancela "
        "con más de 15 días de anticipación se le reembolsa el 100 % de lo pagado "
        "(sin el costo de la pasarela de pagos); entre 7 y 15 días, el 50 %; "
        "y con menos de 7 días no hay reembolso.</p>"
    )
    updated_html = apply_cancellation_policy_to_contract_html(legacy_html)
    assert "20 días calendario" in updated_html
    assert "10 y 19 días calendario" in updated_html
    assert "40 %" in updated_html

    _, mp = _create_verified_musician(db_session, email="frozenpol@example.com")
    c_user = User(
        email="contractor_pol@example.com",
        fullname="Cliente Pol",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(c_user)
    db_session.flush()
    cp = ContractorProfile(user_id=c_user.id, status=ProfileStatus.published)
    db_session.add(cp)
    db_session.flush()

    # Event is 18 days away: under current settings (20d) it would be partial,
    # but if frozen policy at signing had 15d/7d/50%, it must be 100%!
    booking = Booking(
        contractor_id=cp.id,
        musician_id=mp.id,
        event_date=now_peru().date() + timedelta(days=18),
        start_time=time(20, 0),
        end_time=time(22, 0),
        event_type="Serenata",
        location_city="Lima",
        location_address="Av. Arequipa 123",
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("2.00"),
        platform_fee_amount=Decimal("10.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.flush()

    db_session.add(
        Payment(
            booking_id=booking.id,
            amount=Decimal("510.00"),
            payment_type="advance",
            status=PaymentStatus.retained,
        )
    )
    contract = Contract(
        booking_id=booking.id,
        title="Contrato",
        body="<p>Contrato</p>",
        context={"__cancellation_policy__": "15d/7d/50%"},
        musician_signed=True,
        contractor_signed=True,
    )
    db_session.add(contract)
    db_session.commit()

    quote = quote_cancellation(
        db_session,
        booking,
        "contractor",
    )
    assert quote.refund_percent == 100


def test_bug4_12_and_bug35_booking_link_get_and_reset_expiration(client, db_session):
    m_user, mp = _create_verified_musician(db_session, email="linkreset@example.com")
    c_user = User(
        email="clientreset@example.com",
        fullname="Cliente Link",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(c_user)
    db_session.flush()
    cp = ContractorProfile(user_id=c_user.id, status=ProfileStatus.published)
    db_session.add(cp)
    db_session.flush()

    cancelled_booking = Booking(
        contractor_id=cp.id,
        musician_id=mp.id,
        event_date=date.today() + timedelta(days=10),
        start_time=time(19, 0),
        end_time=time(20, 0),
        event_type="Bodas",
        location_city="Lima",
        location_address="Calle 1",
        price_agreed=Decimal("400.00"),
        status=BookingStatus.cancelled,
    )
    db_session.add(cancelled_booking)
    db_session.flush()

    link = BookingLink(
        token="tok-reset-test-123",
        musician_id=mp.id,
        price=Decimal("400.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() - timedelta(hours=2),
        booking_id=cancelled_booking.id,
    )
    db_session.add(link)
    db_session.commit()

    # GET on expired link returns 400 without mutating DB status (BUG-35)
    get_res = client.get(f"/api/v1/booking-links/{link.token}")
    assert get_res.status_code == 400
    db_session.refresh(link)
    assert link.status == BookingLinkStatus.active

    # Reset expiration clears cancelled booking_id and makes GET return 200 (BUG4-12)
    headers = _auth_header(m_user)
    reset_res = client.post(
        f"/api/v1/booking-links/{link.token}/reset-expiration",
        headers=headers,
    )
    assert reset_res.status_code == 200, reset_res.text

    get_after = client.get(f"/api/v1/booking-links/{link.token}")
    assert get_after.status_code == 200, get_after.text


def test_bug5_12_checkout_phone_normalized_uniqueness_for_existing_user(client, db_session):
    _, mp = _create_verified_musician(db_session, email="mus_ph@example.com")
    other_user = User(
        email="other_phone_owner@example.com",
        fullname="Dueño Teléfono",
        phone="999111222",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    existing_client = User(
        email="existing_client@example.com",
        fullname="Cliente Existente",
        phone="988777666",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add_all([other_user, existing_client])
    db_session.flush()
    db_session.add(ContractorProfile(user_id=existing_client.id, status=ProfileStatus.published))

    link = BookingLink(
        token="tok-phone-512",
        musician_id=mp.id,
        price=Decimal("300.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(days=2),
    )
    db_session.add(link)
    db_session.commit()

    headers = _auth_header(existing_client)
    res = client.post(
        f"/api/v1/booking-links/{link.token}/checkout",
        headers=headers,
        json={
            "event_date": (date.today() + timedelta(days=15)).isoformat(),
            "start_time": "20:00:00",
            "event_type": "Cumpleaños",
            "location_city": "Lima",
            "location_address": "Av. Larco 100",
            "fullname": "Cliente Existente",
            "email": existing_client.email,
            "phone": "+51 999 111 222",
            "document_number": "44556677",
        },
    )
    assert res.status_code == 400, res.text
    assert "celular ya está registrado" in res.json()["detail"]


def test_sec4_13_guest_lookup_isolated_and_bug24_musician_notified(client, db_session):
    m_user_a, mp_a = _create_verified_musician(db_session, email="mus_a@example.com")
    _, mp_b = _create_verified_musician(db_session, email="mus_b@example.com")

    headers_a = _auth_header(m_user_a)
    res = client.post(
        "/api/v1/bookings/musician-created",
        headers=headers_a,
        json={
            "contractor_fullname": "Cliente Invitado A",
            "contractor_email": None,
            "contractor_phone": "911222333",
            "document_type": "DNI",
            "document_number": "76543210",
            "event_date": (date.today() + timedelta(days=12)).isoformat(),
            "start_time": "18:00:00",
            "end_time": "19:00:00",
            "event_type": "Aniversario",
            "location_city": "Lima",
            "location_address": "Jr. Unión 200",
            "price_agreed": 450.0,
        },
    )
    assert res.status_code in (200, 201), res.text
    booking_id = res.json()["id"]

    # BUG-24 / BUG3-08: Musician A gets an actionable notification when client has no email
    notif = (
        db_session.query(Notification)
        .filter(
            Notification.user_id == m_user_a.id,
            Notification.type == "booking_created_guest",
        )
        .first()
    )
    assert notif is not None
    assert notif.meta.get("booking_id") == booking_id

    # SEC4-13: Musician B (or musician_id=None) must NOT match Musician A's guest user
    _, user_for_b = resolve_or_create_contractor_client(
        db_session,
        fullname="Otro Nombre",
        email=None,
        phone="911222333",
        document_type="DNI",
        document_number="76543210",
        address="Calle B",
        city="Lima",
        musician_id=mp_b.id,
    )
    _, user_for_none = resolve_or_create_contractor_client(
        db_session,
        fullname="Sin Músico",
        email=None,
        phone="911222333",
        document_type="DNI",
        document_number="76543210",
        address="Calle C",
        city="Lima",
        musician_id=None,
    )
    assert user_for_b.created_by_musician_id == mp_b.id
    assert user_for_none.created_by_musician_id is None
    assert user_for_b.id != user_for_none.id


def test_sec6_11_gcs_client_singleton_and_negative_cache(monkeypatch):
    monkeypatch.setattr(settings, "GCP_BUCKET_NAME", "test-public-bucket")
    monkeypatch.setattr(uploads_mod, "_gcs_client", None)
    uploads_mod._MISSING_GCS_BLOBS.clear()

    client_calls = 0
    exists_calls = 0

    class FakeBlob:
        def exists(self):
            nonlocal exists_calls
            exists_calls += 1
            return False

    class FakeBucket:
        def blob(self, _name: str):
            return FakeBlob()

    class FakeClient:
        def __init__(self):
            nonlocal client_calls
            client_calls += 1

        def bucket(self, _name: str):
            return FakeBucket()

    import google.cloud.storage as gcs_storage

    monkeypatch.setattr(gcs_storage, "Client", FakeClient)

    for _ in range(50):
        assert uploads_mod.read_upload_bytes("/uploads/missing_fase3_test.jpg") is None

    assert client_calls == 1
    assert exists_calls == 1


def test_sec6_12_migrate_private_documents_deletes_public_copy(db_session):
    user = User(
        email="mig_doc@example.com",
        fullname="Usuario Migración",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    public_filename = "legacy_dni_fase3.jpg"
    uploads_mod.ensure_upload_dir()
    public_path = uploads_mod.UPLOAD_DIR / public_filename
    jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    public_path.write_bytes(jpg_bytes)

    cp = ContractorProfile(
        user_id=user.id,
        id_document_url=f"/uploads/{public_filename}",
        status=ProfileStatus.draft,
    )
    db_session.add(cp)
    db_session.commit()

    migrate_url(db_session, cp, "id_document_url", user.id, execute=True)
    db_session.refresh(cp)

    assert cp.id_document_url.startswith("/uploads/private/")
    assert not public_path.exists()
    assert uploads_mod.read_upload_bytes(f"/uploads/{public_filename}") is None
    assert uploads_mod.read_upload_bytes(cp.id_document_url) == jpg_bytes
    priv_filename = cp.id_document_url.removeprefix("/uploads/private/")
    (uploads_mod.PRIVATE_UPLOAD_DIR / priv_filename).unlink(missing_ok=True)
    (uploads_mod.PRIVATE_UPLOAD_DIR / "fase3_sig.png").unlink(missing_ok=True)


def test_sec3_13_calendar_refresh_token_encrypted_and_revoked_on_disconnect(
    client, db_session, monkeypatch
):
    raw_token = "1//0g_test_google_refresh_token_secret"
    encrypted = encrypt_refresh_token(raw_token)
    assert encrypted != raw_token
    assert encrypted.startswith("enc:v1:")
    assert decrypt_refresh_token(encrypted) == raw_token

    m_user, _ = _create_verified_musician(db_session, email="cal_sec313@example.com")
    m_user.google_calendar_refresh_token = encrypted
    db_session.commit()

    revoked_tokens: list[str] = []

    def fake_post(url: str, *, params=None, timeout=None, **kwargs):
        if "oauth2.googleapis.com/revoke" in url:
            revoked_tokens.append((params or {}).get("token"))
        resp = MagicMock()
        resp.status_code = 200
        return resp

    import app.api.v1.endpoints.calendar_auth as cal_auth_mod

    monkeypatch.setattr(cal_auth_mod.httpx, "post", fake_post)

    headers = _auth_header(m_user)
    res = client.delete("/api/v1/calendar-auth/disconnect", headers=headers)
    assert res.status_code == 200, res.text
    assert revoked_tokens == [raw_token]
    db_session.refresh(m_user)
    assert m_user.google_calendar_refresh_token is None


def test_sec15_bug32_and_bug4_08_lifecycle_guards(client, db_session):
    # SEC-15: run_startup_seeds must not call fix_unverified_published_profiles
    assert "fix_unverified_published_profiles" not in (
        migrate_mod.run_startup_seeds.__code__.co_names
    )

    m_user, mp = _create_verified_musician(db_session, email="guards3@example.com")
    c_user = User(
        email="c_guards3@example.com",
        fullname="Contratista Guard",
        role=UserRole.contractor,
        password_hash=get_password_hash("ValidPass123!"),
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(c_user)
    db_session.flush()
    cp = ContractorProfile(user_id=c_user.id, status=ProfileStatus.published)
    db_session.add(cp)
    db_session.flush()

    now = datetime.utcnow()
    b_req = Booking(
        contractor_id=cp.id,
        musician_id=mp.id,
        event_date=date.today() + timedelta(days=20),
        start_time=time(18, 0),
        end_time=time(19, 0),
        event_type="Serenata",
        location_city="Lima",
        location_address="Calle 1",
        status=BookingStatus.requested,
        created_at=now - timedelta(days=4),
    )
    b_unpaid = Booking(
        contractor_id=cp.id,
        musician_id=mp.id,
        event_date=date.today() + timedelta(days=25),
        start_time=time(20, 0),
        end_time=time(21, 0),
        event_type="Boda",
        location_city="Lima",
        location_address="Calle 2",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.accepted,
        quoted_at=now - timedelta(days=8),
        created_at=now - timedelta(days=9),
    )
    db_session.add_all([b_req, b_unpaid])
    db_session.flush()

    # BUG-32: mark_payout_paid on an unpaid (`accepted`) booking must fail with 400
    member = EnsembleMember(
        leader_user_id=m_user.id,
        email="integrante1_fase3@example.com",
        fullname="Integrante 1",
        specialties=["Trompeta"],
    )
    db_session.add(member)
    db_session.flush()
    payout = BookingMemberPayout(
        booking_id=b_unpaid.id,
        ensemble_member_id=member.id,
        amount=Decimal("100.00"),
        status=BookingMemberPayoutStatus.draft,
    )
    db_session.add(payout)
    db_session.commit()

    headers = _auth_header(m_user)
    payout_res = client.post(
        f"/api/v1/bookings/{b_unpaid.id}/member-payouts/{payout.id}/mark-paid",
        headers=headers,
    )
    assert payout_res.status_code == 400, payout_res.text

    # BUG4-08: expire_stale_bookings expires both stale bookings
    stats = expire_stale_bookings(db_session, now=now)
    assert stats["expired_requested"] >= 1
    assert stats["expired_unpaid"] >= 1
    db_session.refresh(b_req)
    db_session.refresh(b_unpaid)
    assert b_req.status == BookingStatus.cancelled
    assert b_unpaid.status == BookingStatus.cancelled
    (uploads_mod.PRIVATE_UPLOAD_DIR / "fase3_sig.png").unlink(missing_ok=True)

