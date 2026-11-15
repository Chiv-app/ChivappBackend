"""Fase 0b: el buscador filtra por tipo, tamaño del grupo, eventos, ciudades adicionales y antelación."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.timezone import now_peru
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _published(
    db: Session,
    name: str,
    *,
    profile_type: str = "solo",
    group_size: str | None = None,
    details: dict | None = None,
    city: str = "Lima",
) -> MusicianProfile:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"m_{suffix}@test.com",
        fullname=name,
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=name,
        slug=f"{name.lower().replace(' ', '-')}-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city=city,
        genres=["Salsa"],
        price_per_event=500,
        profile_type=profile_type,
        group_size=group_size,
        details=details or {},
        profile_image_url="/uploads/x.jpg",
    )
    db.add(profile)
    db.commit()
    return profile


def _search(client: TestClient, **filters) -> list[str]:
    res = client.post("/api/v1/musicians/search", json=filters)
    assert res.status_code == 200, res.text
    return sorted(row["stage_name"] for row in res.json())


def test_filters_by_profile_type_and_group_size(client: TestClient, db_session: Session):
    _published(db_session, "Solista Ana")
    _published(db_session, "Dúo Luna", profile_type="group", group_size="2")
    _published(db_session, "Banda Sol", profile_type="group", group_size="6-8")

    assert _search(client, profile_type="solo") == ["Solista Ana"]
    assert _search(client, profile_type="group") == ["Banda Sol", "Dúo Luna"]
    assert _search(client, profile_type="group", group_sizes=["6-8", "9+"]) == ["Banda Sol"]
    assert _search(client, group_sizes=["2"]) == ["Dúo Luna"]


def test_event_types_require_every_selected_type(client: TestClient, db_session: Session):
    _published(db_session, "Bodas y fiestas", details={"event_types": ["bodas", "cumpleanos"]})
    _published(db_session, "Solo bodas", details={"event_types": ["bodas"]})
    _published(db_session, "Sin datos")

    assert _search(client, event_types=["bodas"]) == ["Bodas y fiestas", "Solo bodas"]
    assert _search(client, event_types=["bodas", "cumpleanos"]) == ["Bodas y fiestas"]
    assert _search(client, event_types=["empresa"]) == []


def test_city_search_also_finds_artists_who_travel_there(client: TestClient, db_session: Session):
    _published(db_session, "De Lima", city="Lima")
    _published(db_session, "Viaja a Arequipa", city="Lima", details={"other_cities": ["Arequipa", "Cusco"]})
    _published(db_session, "De Arequipa", city="Arequipa")

    assert _search(client, city="Arequipa") == ["De Arequipa", "Viaja a Arequipa"]
    assert _search(client, city="arequipa") == ["De Arequipa", "Viaja a Arequipa"]
    assert _search(client, city="Cusco") == ["Viaja a Arequipa"]
    assert _search(client, city="Trujillo") == []


def test_other_cities_match_the_exact_name_not_a_fragment(client: TestClient, db_session: Session):
    _published(db_session, "Viaja a San Isidro", city="Lima", details={"other_cities": ["San Isidro"]})

    assert _search(client, city="Isidro") == []  # «Isidro» solo es un fragmento del nombre guardado
    assert _search(client, city="San Isidro") == ["Viaja a San Isidro"]


def test_wildcards_in_the_city_do_not_match_everything(client: TestClient, db_session: Session):
    _published(db_session, "Viaja", city="Lima", details={"other_cities": ["Arequipa"]})

    assert _search(client, city="%") == []
    assert _search(client, city="_") == []


def test_an_event_that_is_too_soon_hides_artists_who_need_more_notice(client: TestClient, db_session: Session):
    _published(db_session, "Sin antelación")
    _published(db_session, "Mismo día", details={"notice": "0"})
    _published(db_session, "Dos días", details={"notice": "2d"})
    _published(db_session, "Una semana", details={"notice": "1w"})
    _published(db_session, "Un mes", details={"notice": "1m"})

    today = now_peru().date()
    in_three_days = (today + timedelta(days=3)).isoformat()
    in_forty_days = (today + timedelta(days=40)).isoformat()

    assert _search(client, available_date=in_three_days) == ["Dos días", "Mismo día", "Sin antelación"]
    assert _search(client, available_date=in_forty_days) == [
        "Dos días",
        "Mismo día",
        "Sin antelación",
        "Un mes",
        "Una semana",
    ]


def test_invalid_filter_values_are_rejected(client: TestClient):
    for payload in (
        {"profile_type": "banda"},
        {"group_sizes": ["7"]},
        {"event_types": ["fiesta-secreta"]},
    ):
        res = client.post("/api/v1/musicians/search", json=payload)
        assert res.status_code == 422, payload
