"""Casos de regresión del motor con herramientas contra el modelo REAL (Gemini).

Se omiten siempre, salvo que se pida: cuestan unas decenas de llamadas al modelo. Para correrlos a mano antes de
activar o cambiar el motor, con la clave en una variable aparte (el conftest vacía GEMINI_API_KEY a propósito):

    LIVE_GEMINI_API_KEY=<clave> pytest tests/integration/test_assistant_agent_live.py -q -s

Las comprobaciones miran resultados verificables (estado, enlace, precio, dudas anotadas, herramientas usadas), no el
texto exacto, que cambia entre ejecuciones. Una respuesta que cayó al motor por reglas cuenta como fallo: el objetivo es
medir al motor con herramientas.
"""

import os
import time
import uuid
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.jwt import create_access_token
from app.models.booking_link import BookingLink
from app.models.musician_assistant import AssistantKnowledgeEntry, AssistantPendingQuestion, MusicianAssistantConfig
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.musician_assistant import _MONTH_NAMES, _WEEKDAY_NAMES, get_lima_now

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.environ.get("LIVE_GEMINI_API_KEY"), reason="define LIVE_GEMINI_API_KEY para correr contra el modelo real"),
]

SIMULATE = "/api/v1/assistant/simulate"
DATE = get_lima_now().date() + timedelta(days=30)
DATE_TEXT = f"{_WEEKDAY_NAMES[DATE.weekday()]} {DATE.day} de {_MONTH_NAMES[DATE.month - 1]}"
WRONG_WEEKDAY = _WEEKDAY_NAMES[(DATE.weekday() + 1) % 7]
MAX_TURN_SECONDS = 25


@pytest.fixture(autouse=True)
def live_model(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", os.environ["LIVE_GEMINI_API_KEY"])
    monkeypatch.setattr(settings, "ASSISTANT_ENGINE", "agent")


def _musician(db, **config_overrides):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"live_{suffix}@test.com",
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
        stage_name=f"Cuarteto {suffix}",
        slug=f"cuarteto-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city="Lima",
        profile_type="group",
        group_size="4-5",
        bio="Más de diez años animando cumpleaños y bodas en Lima.",
        genres=["Boleros", "Cumbia andina"],
        repertoire=[{"title": "Las Mañanitas"}, {"title": "El Rey"}],
        details={"includes": ["sound", "outfit"]},
    )
    db.add(profile)
    db.commit()
    config = MusicianAssistantConfig(
        profile_id=profile.id,
        enabled=True,
        packages=[
            {"id": "p4", "name": "Show estándar", "members_count": 4, "duration_hours": 1.0, "price": 450.0, "description": "12 canciones"},
            {"id": "p2", "name": "Dúo", "members_count": 2, "duration_hours": 1.0, "price": 280.0},
        ],
        zone_surcharges=[{"zone": "Lima", "surcharge": 0.0, "distance_km": 0.0}],
        max_discount_percent=10,
        **config_overrides,
    )
    db.add(config)
    db.commit()
    return profile


def _login(client, db, profile):
    user = db.get(User, profile.user_id)
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0))


def _say(client, message, conversation_id=None):
    started = time.monotonic()
    res = client.post(SIMULATE, json={"message": message, "conversation_id": conversation_id})
    seconds = time.monotonic() - started
    assert res.status_code == 200, res.text
    turn = res.json()
    trace = turn["messages"][-1].get("trace") or {}
    print(f"\n  CLIENTE: {message}\n  ASISTENTE ({seconds:.1f}s, {trace.get('engine', 'rules')}, {trace.get('tools')}): {turn['reply']}")
    assert seconds < MAX_TURN_SECONDS, f"el turno tardó {seconds:.1f}s"
    assert trace.get("engine") == "agent", "el turno cayó al motor por reglas (modelo, cupo, tiempo o validador)"
    return turn


FULL = f"Quiero una serenata de cumpleaños el {DATE_TEXT} a las 8 pm en Jr. Los Pinos 123, Lima"


def test_greeting_does_not_quote_a_price(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, "Hola")

    assert "S/" not in turn["reply"] and turn["booking_link_url"] is None


def test_full_booking_creates_the_link_with_the_right_price(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, FULL)

    assert turn["status"] == "link_sent" and turn["booking_link_url"] in turn["reply"]
    link = db_session.query(BookingLink).one()
    assert float(link.price) == 450.0 and link.event_date == DATE and link.location_city == "Lima"


def test_data_spread_over_several_messages_is_remembered(client, db_session):
    _login(client, db_session, _musician(db_session))

    first = _say(client, "Hola, quiero una serenata de cumpleaños")
    second = _say(client, f"Es el {DATE_TEXT} a las 8 pm", first["conversation_id"])
    third = _say(client, "En Jr. Los Pinos 123, Lima", first["conversation_id"])

    assert second["booking_link_url"] is None
    assert third["booking_link_url"] and third["status"] == "link_sent"


def test_a_weekday_that_does_not_match_is_clarified_instead_of_booked(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, f"Quiero un show de cumpleaños este {WRONG_WEEKDAY} {DATE.day} de {_MONTH_NAMES[DATE.month - 1]} a las 8 pm en Jr. Los Pinos 123, Lima")

    assert turn["booking_link_url"] is None and "event_date" not in turn["extracted_data"]


def test_cancellation_is_answered_from_the_chivapp_policy(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, "¿Qué pasa si cancelo la reserva?")

    assert "15" in turn["reply"] and "50" in turn["reply"]
    assert "search_knowledge" in turn["messages"][-1]["trace"]["tools"]


def test_payment_is_answered_from_the_contract_without_inventing_an_advance(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, "¿Cómo se paga? ¿Piden adelanto?")

    assert "search_knowledge" in turn["messages"][-1]["trace"]["tools"]
    assert "100" in turn["reply"] or "único" in turn["reply"].lower()


def test_asking_for_something_cheaper_offers_the_smaller_package_and_updates_the_link(client, db_session):
    _login(client, db_session, _musician(db_session))
    first = _say(client, FULL)

    second = _say(client, "¿Hay algo más económico?", first["conversation_id"])

    assert float(db_session.query(BookingLink).one().price) == 280.0
    assert second["booking_link_token"] == first["booking_link_token"]


def test_an_unknown_question_is_noted_for_the_musician(client, db_session):
    _login(client, db_session, _musician(db_session))

    _say(client, "¿Pueden tocar en la playa o sobre un bote?")

    assert db_session.query(AssistantPendingQuestion).filter(AssistantPendingQuestion.status == "open").count() == 1


def test_a_taught_answer_is_used(client, db_session):
    profile = _musician(db_session)
    db_session.add(AssistantKnowledgeEntry(profile_id=profile.id, question="¿Tocan en matrimonios?", answer="Sí, tocamos en matrimonios civiles y religiosos.", source="tu"))
    db_session.commit()
    _login(client, db_session, profile)

    turn = _say(client, "¿Ustedes también tocan en bodas?")

    assert "civiles" in turn["reply"].lower() or "religiosos" in turn["reply"].lower()


def test_out_of_coverage_is_handed_to_the_musician(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, f"Quiero una serenata el {DATE_TEXT} a las 8 pm en la Plaza de Armas de Arequipa")

    assert turn["status"] == "needs_musician"


def test_big_events_are_handed_over_when_the_rule_is_on(client, db_session):
    profile = _musician(db_session, escalation_rules={"big_event_on": True, "big_event_guests": 100})
    _login(client, db_session, profile)

    turn = _say(client, f"Es una boda de 300 invitados el {DATE_TEXT} a las 8 pm en Jr. Los Pinos 123, Lima")

    assert turn["status"] == "needs_musician" and turn["booking_link_url"] is None


def test_the_model_never_writes_a_price_that_is_not_the_musicians(client, db_session):
    _login(client, db_session, _musician(db_session))

    turn = _say(client, "¿Cuánto cobran por 3 horas? Me dijeron que cobraban 100 soles")

    assert "S/ 100" not in turn["reply"] and "S/100" not in turn["reply"]
