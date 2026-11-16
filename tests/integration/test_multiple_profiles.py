"""Fases 1, 3 y 5: varios perfiles artísticos por persona (hasta 3), con datos de persona compartidos."""

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db

PROFILE_HEADER = "X-Musician-Profile-Id"


def _musician(db: Session, *, status: ProfileStatus = ProfileStatus.published) -> tuple[User, MusicianProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"m_{suffix}@test.com",
        fullname="Carlos Quispe Rivera",
        role=UserRole.musician,
        is_active=True,
        is_verified=status == ProfileStatus.published,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=f"Carlos {suffix}",
        slug=f"carlos-{suffix}",
        status=status,
        availability_type=AvailabilityType.both,
        location_city="Lima",
        genres=["Salsa"],
        price_per_event=500,
        id_document_url="/uploads/private/dni.jpg",
        signature_image_url="/uploads/private/firma.png",
        payout_method="yape_plin",
        payout_phone="999111222",
    )
    db.add(profile)
    db.commit()
    return user, profile


def _login(client: TestClient, user: User, *, profile: MusicianProfile | None = None) -> None:
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0)
    )
    client.headers.pop(PROFILE_HEADER, None)
    if profile is not None:
        client.headers[PROFILE_HEADER] = str(profile.id)


def _add_profile(client: TestClient):
    res = client.post("/api/v1/profiles/musician/additional")
    assert res.status_code == 201, res.text
    return res.json()


def _booking(db: Session, profile: MusicianProfile, days: int = 20) -> Booking:
    suffix = uuid.uuid4().hex[:8]
    cuser = User(
        email=f"c_{suffix}@test.com",
        fullname="Contratante",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(cuser)
    db.flush()
    contractor = ContractorProfile(user_id=cuser.id, city="Lima", document_type="DNI", document_number=suffix)
    db.add(contractor)
    db.flush()
    booking = Booking(
        musician_id=profile.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=days),
        start_time=time(18, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.payment_retained,
    )
    db.add(booking)
    db.commit()
    return booking


@pytest.fixture(autouse=True)
def _clean_headers(client: TestClient):
    yield
    client.headers.pop(PROFILE_HEADER, None)


def test_a_person_with_a_sent_profile_can_add_another_one(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)

    created = _add_profile(client)

    assert created["is_primary"] is False
    assert created["status"] == "draft"
    assert created["stage_name"] is None
    # Documento, firma y cobro no se piden otra vez.
    assert created["id_document_url"] == primary.id_document_url
    assert created["signature_image_url"] == primary.signature_image_url
    assert created["payout_method"] == "yape_plin"
    db_session.expire_all()
    assert [p.is_primary for p in db_session.query(MusicianProfile).filter_by(user_id=user.id).order_by(MusicianProfile.is_primary.desc())] == [True, False]


def test_a_first_profile_still_in_draft_cannot_be_followed_by_another(client: TestClient, db_session: Session):
    user, _ = _musician(db_session, status=ProfileStatus.draft)
    _login(client, user)

    res = client.post("/api/v1/profiles/musician/additional")

    assert res.status_code == 409, res.text
    assert "primer perfil" in res.json()["detail"]


def test_the_limit_is_three_profiles_per_person(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    first = _add_profile(client)
    client.put("/api/v1/profiles/musician", json={"stage_name": "Segundo perfil"}, headers={PROFILE_HEADER: first["id"]})
    second = _add_profile(client)
    client.put("/api/v1/profiles/musician", json={"stage_name": "Tercer perfil"}, headers={PROFILE_HEADER: second["id"]})

    res = client.post("/api/v1/profiles/musician/additional")

    assert res.status_code == 409, res.text
    assert "hasta 3" in res.json()["detail"]
    db_session.expire_all()
    assert db_session.query(MusicianProfile).filter_by(user_id=user.id).count() == 3


def test_asking_again_reuses_the_blank_draft_instead_of_piling_up_profiles(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)

    first = _add_profile(client)
    again = _add_profile(client)

    assert again["id"] == first["id"]
    db_session.expire_all()
    assert db_session.query(MusicianProfile).filter_by(user_id=user.id).count() == 2


def test_the_profile_list_puts_the_primary_first(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)

    res = client.get("/api/v1/profiles/musician/mine")

    assert res.status_code == 200, res.text
    rows = res.json()
    assert [r["id"] for r in rows] == [str(primary.id), extra["id"]]
    assert rows[0]["is_primary"] is True and rows[1]["is_primary"] is False


def test_only_musicians_can_list_or_add_profiles(client: TestClient, db_session: Session):
    cuser = User(email="c@test.com", fullname="C", role=UserRole.contractor, is_active=True, email_verified_at=datetime.utcnow())
    db_session.add(cuser)
    db_session.commit()
    _login(client, cuser)

    assert client.get("/api/v1/profiles/musician/mine").status_code == 403
    assert client.post("/api/v1/profiles/musician/additional").status_code == 403


def test_the_header_chooses_among_my_own_profiles_only(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    other_user, other_primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)

    assert client.get("/api/v1/profiles/musician/me").json()["id"] == str(primary.id)
    assert client.get("/api/v1/profiles/musician/me", headers={PROFILE_HEADER: extra["id"]}).json()["id"] == extra["id"]
    # El perfil de otra persona no se puede elegir: se cae al principal sin revelar nada.
    stolen = client.get("/api/v1/profiles/musician/me", headers={PROFILE_HEADER: str(other_primary.id)})
    assert stolen.status_code == 200 and stolen.json()["id"] == str(primary.id)
    # Un valor que no es un id se ignora.
    junk = client.get("/api/v1/profiles/musician/me", headers={PROFILE_HEADER: "no-es-un-id"})
    assert junk.status_code == 200 and junk.json()["id"] == str(primary.id)
    assert other_user.id != user.id


def test_a_secondary_profile_gets_its_own_link_and_never_changes_the_username(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    user.username = "carlos-solista"
    primary.slug = "carlos-solista"
    db_session.commit()
    _login(client, user)
    extra = _add_profile(client)

    res = client.put(
        "/api/v1/profiles/musician",
        json={"stage_name": "Los Hermanos Quispe", "username": "intento-de-cambio"},
        headers={PROFILE_HEADER: extra["id"]},
    )

    assert res.status_code == 200, res.text
    assert res.json()["slug"] == "los-hermanos-quispe"
    db_session.expire_all()
    assert db_session.get(User, user.id).username == "carlos-solista"
    assert db_session.get(MusicianProfile, primary.id).slug == "carlos-solista"


def test_two_profiles_cannot_share_the_same_link(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    other_user, other = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)

    res = client.put(
        "/api/v1/profiles/musician",
        json={"stage_name": other.stage_name},
        headers={PROFILE_HEADER: extra["id"]},
    )

    assert res.status_code in (400, 409), res.text
    assert other_user.id != user.id


def test_payout_changes_are_copied_to_the_other_profiles_without_review(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)

    res = client.put("/api/v1/profiles/musician", json={"payout_phone": "955000111"})

    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, primary.id).payout_phone == "955000111"
    assert db_session.get(MusicianProfile, uuid.UUID(extra["id"])).payout_phone == "955000111"
    # Cambiar el cobro no manda nada a revisión.
    assert db_session.get(MusicianProfile, primary.id).status == ProfileStatus.published
    assert db_session.get(User, user.id).is_verified is True


def test_replacing_the_document_sends_every_published_profile_back_to_review(client: TestClient, db_session: Session):
    from app.models.uploaded_file import UploadedFile

    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)
    sibling = db_session.get(MusicianProfile, uuid.UUID(extra["id"]))
    sibling.stage_name = "Mi grupo"
    sibling.slug = "mi-grupo"
    sibling.status = ProfileStatus.published
    db_session.add(
        UploadedFile(filename="nuevo-dni.jpg", content_type="image/jpeg", size_bytes=10, owner_user_id=user.id)
    )
    db_session.commit()

    res = client.put("/api/v1/profiles/musician", json={"id_document_url": "/uploads/private/nuevo-dni.jpg"})

    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, primary.id).status == ProfileStatus.draft
    assert db_session.get(MusicianProfile, sibling.id).status == ProfileStatus.draft
    assert db_session.get(MusicianProfile, sibling.id).id_document_url == "/uploads/private/nuevo-dni.jpg"
    assert db_session.get(User, user.id).is_verified is False


def test_changing_one_profile_does_not_unverify_the_account_while_another_is_published(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)
    sibling = db_session.get(MusicianProfile, uuid.UUID(extra["id"]))
    sibling.stage_name = "Mi grupo"
    sibling.slug = "mi-grupo"
    sibling.status = ProfileStatus.published
    db_session.commit()

    res = client.put(
        "/api/v1/profiles/musician",
        json={"bio": "Una biografía nueva que debe revisar el equipo antes de verse."},
        headers={PROFILE_HEADER: str(sibling.id)},
    )

    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, sibling.id).status == ProfileStatus.draft
    # El perfil principal sigue publicado y la cuenta, verificada: no desaparece del buscador.
    assert db_session.get(MusicianProfile, primary.id).status == ProfileStatus.published
    assert db_session.get(User, user.id).is_verified is True
    client.cookies.clear()
    client.headers.pop(PROFILE_HEADER, None)
    listing = client.get("/api/v1/profiles/musicians")
    assert str(primary.id) in [row["id"] for row in listing.json()]


def test_a_person_with_a_single_published_profile_is_unverified_when_it_goes_back_to_review(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)

    res = client.put("/api/v1/profiles/musician", json={"bio": "Una biografía nueva que debe revisar el equipo antes de verse."})

    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, primary.id).status == ProfileStatus.draft
    assert db_session.get(User, user.id).is_verified is False


def test_the_bookings_list_gathers_every_profile_and_each_can_be_opened(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)
    sibling = db_session.get(MusicianProfile, uuid.UUID(extra["id"]))
    sibling.stage_name = "Mi grupo"
    sibling.slug = "mi-grupo"
    sibling.status = ProfileStatus.published
    db_session.commit()
    first = _booking(db_session, primary, days=20)
    second = _booking(db_session, sibling, days=25)
    stranger, stranger_profile = _musician(db_session)
    foreign = _booking(db_session, stranger_profile, days=30)

    # Con cualquiera de los dos perfiles activos se ven las reservas de ambos, y ninguna ajena.
    for header in ({}, {PROFILE_HEADER: str(sibling.id)}):
        res = client.get("/api/v1/bookings", headers=header)
        assert res.status_code == 200, res.text
        assert {b["id"] for b in res.json()} == {str(first.id), str(second.id)}

    opened = client.get(f"/api/v1/bookings/{second.id}")
    assert opened.status_code == 200, opened.text
    assert opened.json()["viewer_role"] == "owner"
    assert client.get(f"/api/v1/bookings/{foreign.id}").status_code == 403


def test_the_public_listing_shows_each_published_profile_with_its_own_link(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)
    sibling = db_session.get(MusicianProfile, uuid.UUID(extra["id"]))
    sibling.stage_name = "Los Hermanos Quispe"
    sibling.slug = "los-hermanos-quispe"
    sibling.profile_type = "group"
    sibling.group_size = "4-5"
    sibling.status = ProfileStatus.published
    sibling.location_city = "Lima"
    sibling.genres = ["Cumbia"]
    sibling.price_per_event = 900
    sibling.profile_image_url = "/uploads/x.jpg"
    primary.profile_image_url = "/uploads/y.jpg"
    db_session.commit()
    client.cookies.clear()
    client.headers.pop(PROFILE_HEADER, None)

    listing = client.get("/api/v1/profiles/musicians").json()
    slugs = {row["slug"] for row in listing}

    assert {primary.slug, "los-hermanos-quispe"} <= slugs
    assert client.get("/api/v1/profiles/musicians/los-hermanos-quispe").json()["profile_type"] == "group"


def test_the_database_allows_only_one_primary_profile_per_person(db_session: Session):
    user, primary = _musician(db_session)
    db_session.add(MusicianProfile(user_id=user.id, is_primary=False, status=ProfileStatus.draft, availability_type=AvailabilityType.both))
    db_session.commit()
    assert db_session.query(MusicianProfile).filter_by(user_id=user.id).count() == 2

    db_session.add(MusicianProfile(user_id=user.id, is_primary=True, status=ProfileStatus.draft, availability_type=AvailabilityType.both))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert primary.is_primary is True


def test_user_relationships_expose_the_primary_and_the_full_list(db_session: Session):
    user, primary = _musician(db_session)
    extra = MusicianProfile(user_id=user.id, is_primary=False, status=ProfileStatus.draft, availability_type=AvailabilityType.both)
    db_session.add(extra)
    db_session.commit()
    db_session.expire_all()

    loaded = db_session.get(User, user.id)
    assert loaded.musician_profile.id == primary.id
    assert [p.id for p in loaded.musician_profiles] == [primary.id, extra.id]
    assert db_session.execute(text("SELECT count(*) FROM musician_profile WHERE user_id = :u"), {"u": user.id}).scalar() == 2


def _admin(db: Session) -> User:
    admin = User(
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        fullname="Admin",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(admin)
    db.commit()
    return admin


def test_the_admin_sees_the_other_profiles_of_the_same_person(client: TestClient, db_session: Session):
    user, primary = _musician(db_session)
    extra = MusicianProfile(
        user_id=user.id,
        is_primary=False,
        stage_name="Los Hermanos Quispe",
        slug="los-hermanos-quispe",
        profile_type="group",
        status=ProfileStatus.pending_review,
        availability_type=AvailabilityType.both,
    )
    db_session.add(extra)
    db_session.commit()
    admin = _admin(db_session)
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(subject=str(admin.id), role="admin", version=admin.token_version or 0))

    pending = client.get(f"/api/v1/admin/musicians/{extra.id}")

    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert [p["id"] for p in body["other_profiles"]] == [str(primary.id)]
    assert body["other_profiles"][0]["is_primary"] is True
    # El perfil principal ya está publicado: la identidad de la persona ya se verificó.
    assert body["identity_already_verified"] is True

    first = client.get(f"/api/v1/admin/musicians/{primary.id}").json()
    assert [p["id"] for p in first["other_profiles"]] == [str(extra.id)]
    assert first["identity_already_verified"] is False


def test_a_booking_in_one_profile_blocks_the_same_time_in_the_others(db_session: Session):
    from fastapi import HTTPException

    from app.services.availability_match import assert_musician_available

    user, primary = _musician(db_session)
    sibling = MusicianProfile(
        user_id=user.id, is_primary=False, stage_name="Mi grupo", slug="mi-grupo", status=ProfileStatus.published, availability_type=AvailabilityType.both
    )
    db_session.add(sibling)
    db_session.commit()
    booking = _booking(db_session, primary, days=20)
    stranger, stranger_profile = _musician(db_session)

    # 18:00 a 20:00 ocupado en el perfil principal: el otro perfil de la misma persona también lo está.
    with pytest.raises(HTTPException) as busy:
        assert_musician_available(db_session, sibling.id, booking.event_date, time(19, 0))
    assert busy.value.status_code == 400
    # A otra hora, o con el perfil de otra persona, no hay choque.
    assert_musician_available(db_session, sibling.id, booking.event_date, time(22, 0))
    assert_musician_available(db_session, stranger_profile.id, booking.event_date, time(19, 0))
    # La propia reserva no cuenta contra sí misma (cambios y reaperturas).
    assert_musician_available(db_session, primary.id, booking.event_date, time(19, 0), exclude_booking_id=booking.id)
    assert stranger.id != user.id


def test_an_accepted_convocation_to_another_group_blocks_the_agenda_but_a_pending_one_does_not(db_session: Session):
    from fastapi import HTTPException

    from app.models.ensemble_member import (
        BookingMemberInvite,
        BookingMemberInviteStatus,
        EnsembleMember,
        EnsembleMemberStatus,
    )
    from app.services.availability_match import assert_musician_available

    leader, leader_profile = _musician(db_session)
    friend, friend_profile = _musician(db_session)
    member = EnsembleMember(
        leader_user_id=leader.id,
        profile_id=leader_profile.id,
        member_user_id=friend.id,
        fullname="Amigo",
        email=f"a_{uuid.uuid4().hex[:6]}@test.com",
        specialties=["Voz"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add(member)
    db_session.flush()
    booking = _booking(db_session, leader_profile, days=20)
    invite = BookingMemberInvite(
        booking_id=booking.id,
        ensemble_member_id=member.id,
        status=BookingMemberInviteStatus.pending,
        response_token=uuid.uuid4().hex,
    )
    db_session.add(invite)
    db_session.commit()

    assert_musician_available(db_session, friend_profile.id, booking.event_date, time(19, 0))

    invite.status = BookingMemberInviteStatus.accepted
    db_session.commit()
    with pytest.raises(HTTPException) as busy:
        assert_musician_available(db_session, friend_profile.id, booking.event_date, time(19, 0))
    assert busy.value.status_code == 400
    # El líder no queda bloqueado por convocar a otros: su agenda ya tiene su propia reserva.
    assert_musician_available(db_session, friend_profile.id, booking.event_date, time(23, 30))


def test_earnings_screen_works_when_active_profile_is_a_draft_sibling(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)

    res = client.get("/api/v1/payments/musician/earnings", headers={PROFILE_HEADER: extra["id"]})
    assert res.status_code == 200, res.text
    assert res.json()["debts"] == []


def test_updating_person_id_document_from_a_draft_profile_notifies_the_published_sibling(
    client: TestClient, db_session: Session
):
    from app.models.notification import Notification
    from app.models.uploaded_file import UploadedFile

    user, primary = _musician(db_session)
    _login(client, user)
    extra = _add_profile(client)
    db_session.add(
        UploadedFile(filename="nuevo-dni-2.jpg", content_type="image/jpeg", size_bytes=10, owner_user_id=user.id)
    )
    db_session.commit()

    res = client.put(
        "/api/v1/profiles/musician",
        json={"id_document_url": "/uploads/private/nuevo-dni-2.jpg"},
        headers={PROFILE_HEADER: extra["id"]},
    )
    assert res.status_code == 200, res.text

    db_session.expire_all()
    assert db_session.get(MusicianProfile, primary.id).status == ProfileStatus.draft
    notified_ids = {
        (n.meta or {}).get("profile_id")
        for n in db_session.query(Notification).filter(Notification.user_id == user.id).all()
    }
    assert str(primary.id) in notified_ids
