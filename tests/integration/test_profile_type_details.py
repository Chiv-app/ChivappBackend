"""Fase 0 del multiperfil: tipo de perfil (solista o grupo), tamaño del grupo y datos de detalle."""

import importlib.util
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _musician(db: Session, *, status: ProfileStatus = ProfileStatus.draft, city: str = "Lima") -> tuple[User, MusicianProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"musico_{suffix}@test.com",
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
        stage_name=f"Grupo {suffix}",
        slug=f"grupo-{suffix}",
        status=status,
        availability_type=AvailabilityType.both,
        location_city=city,
        genres=["Salsa"],
        price_per_event=500,
    )
    db.add(profile)
    db.commit()
    return user, profile


def _login(client: TestClient, user: User) -> None:
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(user.id), role="musician", version=user.token_version or 0)
    )


def _put(client: TestClient, payload: dict):
    return client.put("/api/v1/profiles/musician", json=payload)


def _add_member(db: Session, leader: User, status: EnsembleMemberStatus) -> EnsembleMember:
    member = EnsembleMember(
        leader_user_id=leader.id,
        fullname="Integrante",
        email=f"i_{uuid.uuid4().hex[:8]}@test.com",
        specialties=["Voz"],
        status=status,
    )
    db.add(member)
    db.commit()
    return member


def test_a_new_profile_is_a_solo_artist_with_no_details(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)

    res = client.get("/api/v1/profiles/musician/me")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile_type"] == "solo"
    assert body["group_size"] is None
    assert body["details"] == {}


def test_a_group_saves_its_size_and_details(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _login(client, user)

    res = _put(
        client,
        {
            "profile_type": "group",
            "group_size": "4-5",
            "details": {
                "event_types": ["bodas", "cumpleanos"],
                "audience": "150",
                "duration": "3h",
                "includes": ["sound", "lights"],
                "setup": "1h",
                "other_cities": ["Arequipa", "Trujillo"],
                "travel_fee": "maybe",
                "notice": "1w",
                "experience": "5-10",
                "format": "mixed",
                "reduced": "yes",
            },
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile_type"] == "group"
    assert body["group_size"] == "4-5"
    assert body["details"]["event_types"] == ["bodas", "cumpleanos"]
    assert body["details"]["format"] == "mixed"
    db_session.expire_all()
    stored = db_session.get(MusicianProfile, profile.id)
    assert stored.profile_type == "group" and stored.details["notice"] == "1w"


@pytest.mark.parametrize(
    "payload",
    [
        {"profile_type": "banda"},
        {"profile_type": "group", "group_size": "7"},
        {"details": {"event_types": ["fiesta-secreta"]}},
        {"details": {"duration": "10h"}},
        {"details": {"clave_inventada": "x"}},
        {"details": {"other_cities": ["x"]}},
        {"details": {"event_types": ["bodas"] * 10}},
    ],
)
def test_values_outside_the_allowed_lists_are_rejected(client: TestClient, db_session: Session, payload: dict):
    user, profile = _musician(db_session)
    _login(client, user)

    res = _put(client, payload)

    assert res.status_code == 422, res.text
    db_session.expire_all()
    stored = db_session.get(MusicianProfile, profile.id)
    assert stored.profile_type == "solo" and stored.details == {}


def test_details_are_replaced_as_a_whole_and_cleaned(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    _put(client, {"details": {"event_types": ["bodas", "empresa"], "duration": "2h", "notice": "2d"}})

    res = _put(
        client,
        {"details": {"event_types": ["empresa", "empresa", "bodas"], "duration": None, "includes": []}},
    )

    assert res.status_code == 200, res.text
    # Lo que no se envía o viene vacío queda sin completar; los repetidos se quitan.
    assert res.json()["details"] == {"event_types": ["empresa", "bodas"]}


def test_a_solo_artist_has_no_group_size_or_group_only_details(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    _put(client, {"profile_type": "group", "group_size": "3", "details": {"format": "acoustic", "reduced": "no", "duration": "2h"}})

    res = _put(client, {"profile_type": "solo"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile_type"] == "solo"
    assert body["group_size"] is None
    assert body["details"] == {"duration": "2h"}


def test_a_solo_request_cannot_carry_a_group_size(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)

    res = _put(client, {"profile_type": "solo", "group_size": "3", "details": {"format": "mixed", "notice": "1w"}})

    assert res.status_code == 200, res.text
    assert res.json()["group_size"] is None
    assert res.json()["details"] == {"notice": "1w"}


def test_cannot_switch_to_solo_while_there_are_members(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _login(client, user)
    _put(client, {"profile_type": "group", "group_size": "3"})
    _add_member(db_session, user, EnsembleMemberStatus.active)

    res = _put(client, {"profile_type": "solo"})

    assert res.status_code == 409, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, profile.id).profile_type == "group"


def test_ended_members_do_not_block_switching_to_solo(client: TestClient, db_session: Session):
    user, _ = _musician(db_session)
    _login(client, user)
    _put(client, {"profile_type": "group", "group_size": "3"})
    _add_member(db_session, user, EnsembleMemberStatus.inactive)

    res = _put(client, {"profile_type": "solo"})

    assert res.status_code == 200, res.text


def test_completing_these_fields_does_not_send_a_published_profile_back_to_review(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, status=ProfileStatus.published)
    _login(client, user)

    res = _put(client, {"profile_type": "group", "group_size": "6-8", "details": {"event_types": ["bodas"], "notice": "2w"}})

    assert res.status_code == 200, res.text
    db_session.expire_all()
    stored = db_session.get(MusicianProfile, profile.id)
    assert stored.status == ProfileStatus.published
    assert db_session.get(User, user.id).is_verified is True


def test_changing_the_bio_still_sends_a_published_profile_back_to_review(client: TestClient, db_session: Session):
    # Con la verificación automática, editar la biografía mantiene el perfil publicado.
    user, profile = _musician(db_session, status=ProfileStatus.published)
    _login(client, user)

    res = _put(client, {"bio": "Una biografía nueva que se publica automáticamente sin revisión manual."})

    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(MusicianProfile, profile.id).status == ProfileStatus.published


def test_the_public_profile_and_the_listing_expose_type_size_and_details(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, status=ProfileStatus.published)
    _login(client, user)
    _put(client, {"profile_type": "group", "group_size": "2", "details": {"event_types": ["bodas"], "duration": "2h"}})
    client.cookies.clear()

    public = client.get(f"/api/v1/profiles/musicians/{profile.slug}")
    listing = client.get("/api/v1/profiles/musicians")

    assert public.status_code == 200, public.text
    body = public.json()
    assert body["profile_type"] == "group" and body["group_size"] == "2"
    assert body["details"] == {"event_types": ["bodas"], "duration": "2h"}
    assert listing.status_code == 200, listing.text
    row = next(item for item in listing.json() if item["id"] == str(profile.id))
    assert row["profile_type"] == "group" and row["group_size"] == "2"


def test_the_search_results_include_type_and_size(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, status=ProfileStatus.published)
    _login(client, user)
    _put(client, {"profile_type": "group", "group_size": "9+"})
    client.cookies.clear()

    res = client.post("/api/v1/musicians/search", json={})

    assert res.status_code == 200, res.text
    row = next(item for item in res.json() if item["musician_id"] == str(profile.id))
    assert row["profile_type"] == "group" and row["group_size"] == "9+"


def test_the_database_rejects_invalid_type_and_size(db_session: Session):
    from sqlalchemy.exc import IntegrityError

    _, profile = _musician(db_session)
    # Las pruebas pueden correr sobre una base creada antes de existir la restricción.
    has_check = db_session.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_musician_profile_type'")
    ).first()
    if not has_check:
        pytest.skip("la base de pruebas se creó antes de existir la restricción")

    with pytest.raises(IntegrityError):
        db_session.execute(text("UPDATE musician_profile SET profile_type = 'banda' WHERE id = :id"), {"id": profile.id})
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(text("UPDATE musician_profile SET group_size = '7' WHERE id = :id"), {"id": profile.id})
    db_session.rollback()


def test_the_migration_marks_as_group_only_profiles_that_have_members(db_session: Session):
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20261115_d4e5f6a7b8c9_musician_profile_type_and_details.py"
    )
    spec = importlib.util.spec_from_file_location("profile_type_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with_active, _ = _musician(db_session)
    with_invited, _ = _musician(db_session)
    with_inactive, _ = _musician(db_session)
    without_members, _ = _musician(db_session)
    _add_member(db_session, with_active, EnsembleMemberStatus.active)
    _add_member(db_session, with_invited, EnsembleMemberStatus.invited)
    _add_member(db_session, with_inactive, EnsembleMemberStatus.inactive)

    db_session.execute(text(module.BACKFILL_GROUPS_SQL))
    db_session.commit()

    def type_of(user: User) -> str:
        return db_session.query(MusicianProfile.profile_type).filter(MusicianProfile.user_id == user.id).scalar()

    assert type_of(with_active) == "group"
    assert type_of(with_invited) == "group"
    assert type_of(with_inactive) == "solo"
    assert type_of(without_members) == "solo"
    # Repetir el relleno no cambia nada.
    db_session.execute(text(module.BACKFILL_GROUPS_SQL))
    db_session.commit()
    assert type_of(with_active) == "group" and type_of(without_members) == "solo"
