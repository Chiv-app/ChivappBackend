"""Fase 2: el equipo pertenece a un perfil (grupo) y se nota, con discreción, en los perfiles públicos."""

import importlib.util
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db

PROFILE_HEADER = "X-Musician-Profile-Id"


def _user(db: Session, name: str = "Carlos Quispe") -> User:
    user = User(
        email=f"u_{uuid.uuid4().hex[:8]}@test.com",
        fullname=name,
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    return user


def _profile(db: Session, user: User, name: str, *, kind: str = "solo", primary: bool = True, status=ProfileStatus.published) -> MusicianProfile:
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=primary,
        stage_name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        status=status,
        availability_type=AvailabilityType.both,
        location_city="Lima",
        genres=["Salsa"],
        price_per_event=500,
        profile_type=kind,
        profile_image_url="/uploads/x.jpg",
    )
    db.add(profile)
    db.flush()
    return profile


def _member(db: Session, leader: User, profile: MusicianProfile | None, who: User | None, name: str, *, status=EnsembleMemberStatus.active) -> EnsembleMember:
    member = EnsembleMember(
        leader_user_id=leader.id,
        profile_id=profile.id if profile else None,
        member_user_id=who.id if who else None,
        fullname=name,
        email=f"{uuid.uuid4().hex[:8]}@test.com",
        specialties=["Voz"],
        status=status,
    )
    db.add(member)
    db.flush()
    return member


def _login(client: TestClient, user: User, profile: MusicianProfile | None = None) -> None:
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(subject=str(user.id), role="musician", version=user.token_version or 0))
    client.headers.pop(PROFILE_HEADER, None)
    if profile is not None:
        client.headers[PROFILE_HEADER] = str(profile.id)


@pytest.fixture(autouse=True)
def _clean_headers(client: TestClient):
    yield
    client.headers.pop(PROFILE_HEADER, None)


def test_each_group_profile_has_its_own_team(client: TestClient, db_session: Session):
    leader = _user(db_session)
    solo = _profile(db_session, leader, "Carlos solista")
    group_a = _profile(db_session, leader, "Los Hermanos Quispe", kind="group", primary=False)
    group_b = _profile(db_session, leader, "Orquesta Luna", kind="group", primary=False)
    _member(db_session, leader, group_a, None, "Ana de los Hermanos")
    _member(db_session, leader, group_b, None, "Beto de la Orquesta")
    db_session.commit()
    _login(client, leader, group_a)

    mine = client.get("/api/v1/musician/members")
    assert mine.status_code == 200, mine.text
    assert [m["fullname"] for m in mine.json()] == ["Ana de los Hermanos"]

    client.headers[PROFILE_HEADER] = str(group_b.id)
    assert [m["fullname"] for m in client.get("/api/v1/musician/members").json()] == ["Beto de la Orquesta"]
    client.headers[PROFILE_HEADER] = str(solo.id)
    assert client.get("/api/v1/musician/members").json() == []


def test_members_from_before_this_change_belong_to_the_primary_profile(client: TestClient, db_session: Session):
    leader = _user(db_session)
    primary = _profile(db_session, leader, "Grupo viejo", kind="group")
    other = _profile(db_session, leader, "Otro grupo", kind="group", primary=False)
    _member(db_session, leader, None, None, "Integrante antiguo")  # sin perfil: dato de antes
    db_session.commit()

    _login(client, leader, primary)
    assert [m["fullname"] for m in client.get("/api/v1/musician/members").json()] == ["Integrante antiguo"]
    _login(client, leader, other)
    assert client.get("/api/v1/musician/members").json() == []


def test_a_new_member_joins_the_active_profile_and_may_also_join_another(client: TestClient, db_session: Session):
    leader = _user(db_session)
    _profile(db_session, leader, "Principal", kind="group")
    other = _profile(db_session, leader, "Segundo grupo", kind="group", primary=False)
    db_session.commit()
    body = {"email": "ana@example.com", "fullname": "Ana Torres", "specialties": ["Voz"]}

    _login(client, leader, other)
    first = client.post("/api/v1/musician/members", json=body)
    assert first.status_code == 201, first.text
    db_session.expire_all()
    assert db_session.query(EnsembleMember).filter_by(fullname="Ana Torres").one().profile_id == other.id
    # La misma persona no puede estar dos veces en el mismo perfil...
    assert client.post("/api/v1/musician/members", json=body).status_code == 400
    # ...pero sí en otro grupo del mismo líder.
    _login(client, leader)
    assert client.post("/api/v1/musician/members", json=body).status_code == 201


def test_a_solo_profile_cannot_invite_members(client: TestClient, db_session: Session):
    leader = _user(db_session)
    solo = _profile(db_session, leader, "Solista")
    db_session.commit()
    _login(client, leader, solo)

    res = client.post("/api/v1/musician/members", json={"email": "x@example.com", "fullname": "Xavier Díaz", "specialties": ["Voz"]})

    assert res.status_code == 400, res.text
    assert "perfiles de grupo" in res.json()["detail"]
    assert db_session.query(EnsembleMember).count() == 0


def test_switching_to_solo_only_counts_the_members_of_that_profile(client: TestClient, db_session: Session):
    leader = _user(db_session)
    _profile(db_session, leader, "Principal", kind="group")
    other = _profile(db_session, leader, "Con equipo", kind="group", primary=False)
    free = _profile(db_session, leader, "Sin equipo", kind="group", primary=False)
    _member(db_session, leader, other, None, "Integrante")
    db_session.commit()

    _login(client, leader, other)
    assert client.put("/api/v1/profiles/musician", json={"profile_type": "solo"}).status_code == 409
    _login(client, leader, free)
    assert client.put("/api/v1/profiles/musician", json={"profile_type": "solo"}).status_code == 200


def _booking(db: Session, profile: MusicianProfile) -> Booking:
    cuser = User(email=f"c_{uuid.uuid4().hex[:8]}@test.com", fullname="Contratante", role=UserRole.contractor, is_active=True, is_verified=True, email_verified_at=datetime.utcnow())
    db.add(cuser)
    db.flush()
    contractor = ContractorProfile(user_id=cuser.id, city="Lima", document_type="DNI", document_number=uuid.uuid4().hex[:8])
    db.add(contractor)
    db.flush()
    booking = Booking(
        musician_id=profile.id,
        contractor_id=contractor.id,
        event_date=now_peru().date() + timedelta(days=20),
        start_time=time(18, 0),
        location_address="Jr. Ayacucho 123",
        event_type="Serenata",
        price_agreed=Decimal("600.00"),
        status=BookingStatus.payment_retained,
    )
    db.add(booking)
    db.commit()
    return booking


def test_only_members_of_the_bookings_profile_can_be_convoked(client: TestClient, db_session: Session):
    leader = _user(db_session)
    _profile(db_session, leader, "Principal", kind="group")
    group_a = _profile(db_session, leader, "Grupo A", kind="group", primary=False)
    group_b = _profile(db_session, leader, "Grupo B", kind="group", primary=False)
    member_a = _member(db_session, leader, group_a, None, "Del grupo A")
    member_b = _member(db_session, leader, group_b, None, "Del grupo B")
    booking = _booking(db_session, group_a)
    _login(client, leader, group_a)

    wrong = client.post(f"/api/v1/bookings/{booking.id}/member-invites", json={"ensemble_member_ids": [str(member_b.id)]})
    assert wrong.status_code == 400, wrong.text
    assert "no es integrante de este perfil" in wrong.json()["detail"]

    right = client.post(f"/api/v1/bookings/{booking.id}/member-invites", json={"ensemble_member_ids": [str(member_a.id)]})
    assert right.status_code == 201, right.text


def test_member_payouts_reject_members_from_another_group_and_show_group_stage_name(
    client: TestClient, db_session: Session
):
    from app.models.contractor_recommendation import ContractorRecommendation

    leader = _user(db_session, "Carlos Quispe")
    group_a = _profile(db_session, leader, "Mariachi Sol", kind="group")
    group_b = _profile(db_session, leader, "Trío Los Andes", kind="group", primary=False)
    friend = _user(db_session, "Luis Pérez")
    member_a = _member(db_session, leader, group_a, friend, "Luis Pérez")
    member_b = _member(db_session, leader, group_b, friend, "Luis Pérez")
    booking = _booking(db_session, group_a)
    booking.status = BookingStatus.completed
    db_session.add(
        ContractorRecommendation(
            booking_id=booking.id,
            musician_id=group_a.id,
            contractor_id=booking.contractor_id,
            rating=5,
            comment="Excelente presentación",
        )
    )
    db_session.commit()

    _login(client, leader, group_a)
    wrong = client.put(
        f"/api/v1/bookings/{booking.id}/member-payouts",
        json={"items": [{"ensemble_member_id": str(member_b.id), "amount": "100.00"}], "lock": False},
    )
    assert wrong.status_code == 400, wrong.text
    assert "no es integrante de este perfil" in wrong.json()["detail"]

    right = client.put(
        f"/api/v1/bookings/{booking.id}/member-payouts",
        json={"items": [{"ensemble_member_id": str(member_a.id), "amount": "100.00"}], "lock": False},
    )
    assert right.status_code == 200, right.text

    # El integrante ve el nombre artístico de cada grupo, no solo el nombre de la persona líder.
    _login(client, friend)
    ensembles = client.get("/api/v1/musician/my-ensembles").json()
    assert {e["leader_name"] for e in ensembles} == {"Mariachi Sol", "Trío Los Andes"}
    income = client.get("/api/v1/musician/my-income").json()
    assert [i["leader_name"] for i in income["items"]] == ["Mariachi Sol"]


def test_the_group_page_lists_active_members_with_a_discreet_link_to_their_solo_profile(client: TestClient, db_session: Session):
    leader = _user(db_session)
    group = _profile(db_session, leader, "Los Hermanos Quispe", kind="group")
    friend = _user(db_session, "Luis Pérez")
    friend_solo = _profile(db_session, friend, "Luis Pérez solista")
    plain = _user(db_session, "Marta Ríos")
    _profile(db_session, plain, "Marta", status=ProfileStatus.draft)
    _member(db_session, leader, group, friend, "Luis Pérez")
    _member(db_session, leader, group, plain, "Marta Ríos")
    _member(db_session, leader, group, None, "Sin cuenta", status=EnsembleMemberStatus.invited)
    _member(db_session, leader, group, None, "Dado de baja", status=EnsembleMemberStatus.inactive)
    db_session.commit()

    page = client.get(f"/api/v1/profiles/musicians/{group.slug}").json()

    assert [m["name"] for m in page["members"]] == ["Luis Pérez", "Marta Ríos"]
    by_name = {m["name"]: m for m in page["members"]}
    assert by_name["Luis Pérez"]["profile_slug"] == friend_solo.slug
    assert by_name["Marta Ríos"]["profile_slug"] is None  # su perfil aún no está publicado
    # Nunca se exponen contactos.
    assert set(page["members"][0]) == {"name", "specialties", "profile_slug"}


def test_the_solo_page_says_which_groups_the_person_plays_with(client: TestClient, db_session: Session):
    leader = _user(db_session)
    group = _profile(db_session, leader, "Los Hermanos Quispe", kind="group")
    draft_group = _profile(db_session, leader, "Grupo sin publicar", kind="group", primary=False, status=ProfileStatus.draft)
    friend = _user(db_session, "Luis Pérez")
    friend_solo = _profile(db_session, friend, "Luis solista")
    _member(db_session, leader, group, friend, "Luis Pérez")
    _member(db_session, leader, draft_group, friend, "Luis Pérez")
    _member(db_session, leader, None, friend, "Luis (invitado)", status=EnsembleMemberStatus.invited)
    db_session.commit()

    page = client.get(f"/api/v1/profiles/musicians/{friend_solo.slug}").json()

    assert page["plays_with"] == [{"name": "Los Hermanos Quispe", "slug": group.slug}]
    # Un grupo no lista «toca con» y un solista no lista integrantes.
    assert page["members"] == []
    assert client.get(f"/api/v1/profiles/musicians/{group.slug}").json()["plays_with"] == []


def test_the_migration_puts_existing_members_in_the_leaders_primary_profile(db_session: Session):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "20261116_f6a7b8c9d0e1_ensemble_member_profile.py"
    spec = importlib.util.spec_from_file_location("member_profile_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    leader = _user(db_session)
    primary = _profile(db_session, leader, "Principal", kind="group")
    _profile(db_session, leader, "Adicional", kind="group", primary=False)
    old = _member(db_session, leader, None, None, "Antiguo")
    already = _member(db_session, leader, _profile(db_session, leader, "Tercero", kind="group", primary=False), None, "Ya asignado")
    kept = already.profile_id
    db_session.commit()

    db_session.execute(text(module.BACKFILL_MEMBER_PROFILE_SQL))
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(EnsembleMember, old.id).profile_id == primary.id
    assert db_session.get(EnsembleMember, already.id).profile_id == kept
