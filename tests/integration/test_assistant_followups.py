"""Asistente IA: preguntas posteriores al enlace, «algo más económico», saludo sin precio y formato de WhatsApp."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.models.musician_assistant import MusicianAssistantConfig
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.musician_assistant import _MONTH_NAMES, _WEEKDAY_NAMES, get_lima_now

pytestmark = pytest.mark.db

SIMULATE = "/api/v1/assistant/simulate"
# Fecha a 30 días de hoy: las pruebas no dependen del día en que se corren.
EVENT_DATE = get_lima_now().date() + timedelta(days=30)
EVENT_DAY = f"{EVENT_DATE.day} de {_MONTH_NAMES[EVENT_DATE.month - 1]}"
EVENT_HUMAN = f"{_WEEKDAY_NAMES[EVENT_DATE.weekday()]} {EVENT_DAY}"
WRONG_WEEKDAY = _WEEKDAY_NAMES[(EVENT_DATE.weekday() + 1) % 7]
FIRST = f"Quiero una serenata de cumpleaños el {EVENT_DAY} a las 8 pm en Jr Quinua 123, Miraflores"


def _group_musician(db: Session) -> MusicianProfile:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"f_{suffix}@test.com",
        fullname="Carlos",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=f"Grupo Test {suffix}",
        slug=f"grupo-test-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city="Lima",
        profile_type="group",
        group_size="6-8",
        bio="Más de diez años animando celebraciones familiares.",
        genres=["Mariachi", "Boleros"],
        repertoire=[{"title": "Las Mañanitas"}, {"title": "El Rey"}],
        details={"includes": ["sound", "outfit"], "setup": "30m"},
    )
    db.add(profile)
    db.commit()
    db.add(
        MusicianAssistantConfig(
            profile_id=profile.id,
            enabled=True,
            packages=[
                {"id": "p7", "name": "Show estándar (7 integrantes)", "members_count": 7, "duration_hours": 1.0, "price": 450.0},
                {"id": "p6", "name": "Formato reducido (6 integrantes)", "members_count": 6, "duration_hours": 1.0, "price": 380.0},
            ],
            zone_surcharges=[{"zone": "Lima", "surcharge": 0.0, "distance_km": 0.0}],
            max_discount_percent=10,
        )
    )
    db.commit()
    return profile


def _login(client: TestClient, profile: MusicianProfile, db: Session) -> None:
    user = db.get(User, profile.user_id)
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0),
    )


def _say(client: TestClient, text: str, conversation_id: str | None = None) -> dict:
    res = client.post(SIMULATE, json={"message": text, "conversation_id": conversation_id})
    assert res.status_code == 200, res.text
    return res.json()


def _with_link(client: TestClient) -> dict:
    turn = _say(client, FIRST)
    assert turn["booking_link_url"], turn["reply"]
    return turn


def test_greeting_does_not_quote_a_price(client, db_session):
    _login(client, _group_musician(db_session), db_session)

    turn = _say(client, "Hola")

    assert "S/" not in turn["reply"]
    assert "?" in turn["reply"] or "cuéntame" in turn["reply"].lower()


def test_link_message_uses_human_date_and_whatsapp_bold(client, db_session):
    _login(client, _group_musician(db_session), db_session)

    turn = _with_link(client)

    assert EVENT_HUMAN in turn["reply"]
    assert EVENT_DATE.isoformat() not in turn["reply"]
    assert "**" not in turn["reply"]


def test_question_after_link_is_answered_not_repeated(client, db_session):
    _login(client, _group_musician(db_session), db_session)
    first = _with_link(client)

    turn = _say(client, "Antes de contratar, explícame qué contiene su show", first["conversation_id"])

    reply = turn["reply"]
    assert "Excelente" not in reply
    assert "equipo de sonido" in reply
    assert "Las Mañanitas" in reply
    assert first["booking_link_url"] in reply
    assert turn["booking_link_url"] == first["booking_link_url"]


def test_thanks_after_link_gets_a_short_reply(client, db_session):
    _login(client, _group_musician(db_session), db_session)
    first = _with_link(client)

    turn = _say(client, "Gracias", first["conversation_id"])

    assert "Cotización" not in turn["reply"]
    assert first["booking_link_url"] in turn["reply"]


def test_asking_for_something_cheaper_offers_the_smaller_package_before_a_discount(client, db_session):
    _login(client, _group_musician(db_session), db_session)
    first = _with_link(client)
    assert first["quote_breakdown"]["final_price"] == 450.0

    turn = _say(client, "hay algo menos?", first["conversation_id"])

    assert turn["quote_breakdown"]["final_price"] == 380.0
    assert turn["quote_breakdown"]["discount_applied"] == 0.0
    assert "Formato reducido" in turn["reply"]
    assert turn["booking_link_token"] == first["booking_link_token"]


def test_explicit_discount_request_still_applies_the_configured_discount(client, db_session):
    _login(client, _group_musician(db_session), db_session)
    first = _with_link(client)

    turn = _say(client, "¿me haces un descuento?", first["conversation_id"])

    assert turn["quote_breakdown"]["discount_applied"] == 45.0
    assert turn["quote_breakdown"]["final_price"] == 405.0


def test_weekday_that_does_not_match_the_date_is_clarified_not_booked(client, db_session):
    _login(client, _group_musician(db_session), db_session)

    turn = _say(client, f"Tengo un cumpleaños este {WRONG_WEEKDAY} {EVENT_DATE.day} {_MONTH_NAMES[EVENT_DATE.month - 1]} a las 8pm en Jr Quinua 123, Miraflores")

    assert turn["booking_link_url"] is None
    assert EVENT_HUMAN in turn["reply"]
    assert "¿Te refieres" in turn["reply"]
    assert "event_date" not in turn["extracted_data"]

    answer = _say(client, f"El {EVENT_HUMAN}, a las 8 pm en Jr Quinua 123, Miraflores", turn["conversation_id"])

    assert answer["booking_link_url"]
    assert "date_conflict" not in answer["extracted_data"]


def test_address_without_a_district_asks_for_it_before_creating_the_link(client, db_session):
    _login(client, _group_musician(db_session), db_session)

    turn = _say(client, f"Es un cumpleaños el {EVENT_DAY} a las 8pm en Jr Quinua")

    assert turn["booking_link_url"] is None
    assert "distrito" in turn["reply"].lower()

    answer = _say(client, "Comas", turn["conversation_id"])

    assert answer["booking_link_url"]
    assert answer["extracted_data"]["location_city"] == "Comas"
