"""Motor del asistente con memoria y herramientas (`ASSISTANT_ENGINE=agent`).

El modelo se sustituye por uno guionado: devuelve llamadas a herramientas y respuestas según lo que ve en los
pasos que recibe. Así se prueba todo el bucle (herramientas reales, base de datos real, validador, cupos y
respaldo por reglas) sin llamar a Gemini ni gastar nada.
"""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

import app.services.assistant_agent as agent
import app.services.musician_assistant as assistant_svc
from app.core.config import settings
from app.core.jwt import create_access_token
from app.models.booking_link import BookingLink
from app.models.musician_assistant import AssistantConversation, MusicianAssistantConfig
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.notification import Notification
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.assistant_usage import get_global_calls_today, get_or_create_settings_row, get_user_usage
from app.services.musician_assistant import _MONTH_NAMES, _WEEKDAY_NAMES, get_lima_now

pytestmark = pytest.mark.db

SIMULATE = "/api/v1/assistant/simulate"
EVENT_DATE = get_lima_now().date() + timedelta(days=30)
WRONG_WEEKDAY = _WEEKDAY_NAMES[(EVENT_DATE.weekday() + 1) % 7]


def _musician(db) -> MusicianProfile:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"a_{suffix}@test.com",
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
        stage_name=f"Grupo {suffix}",
        slug=f"grupo-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city="Lima",
    )
    db.add(profile)
    db.commit()
    db.add(
        MusicianAssistantConfig(
            profile_id=profile.id,
            enabled=True,
            packages=[{"id": "p", "name": "Show estándar", "members_count": 4, "duration_hours": 1.0, "price": 450.0}],
            zone_surcharges=[{"zone": "Lima", "surcharge": 0.0, "distance_km": 0.0}],
        )
    )
    db.commit()
    return profile


def _login(client, db, profile) -> None:
    user = db.get(User, profile.user_id)
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0)
    )


def _say(client, message, conversation_id=None) -> dict:
    res = client.post(SIMULATE, json={"message": message, "conversation_id": conversation_id})
    assert res.status_code == 200, res.text
    return res.json()


def _calls(*calls):
    """Respuesta del modelo que pide herramientas: cada llamada es (nombre, argumentos)."""
    return {
        "status": "requires_action",
        "steps": [{"type": "thought", "signature": "firma"}]
        + [
            {"type": "function_call", "id": f"call_{i}", "name": name, "arguments": args}
            for i, (name, args) in enumerate(calls)
        ],
    }


def _text(value: str):
    return {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": value}]}]}


def _result_of(steps, tool_name):
    for step in steps:
        if step.get("type") == "function_result" and step.get("name") == tool_name:
            return json.loads(step["result"][0]["text"])
    return None


@pytest.fixture
def engine(monkeypatch):
    """Activa el motor con herramientas y reemplaza al modelo por un guion. Devuelve lo que el modelo recibió."""
    monkeypatch.setattr(settings, "ASSISTANT_ENGINE", "agent")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "clave-de-prueba")
    # El respaldo por reglas tampoco debe llamar al modelo real.
    monkeypatch.setattr(assistant_svc, "_call_gemini_interaction", lambda **_kw: None)
    seen: list[dict] = []

    class Script:
        def __init__(self):
            self.script = None

        def use(self, script):
            self.script = script

    holder = Script()

    def fake(system_prompt, steps):
        seen.append({"system": system_prompt, "steps": steps})
        return holder.script(system_prompt, steps)

    monkeypatch.setattr(agent, "_interactions_call", fake)
    holder.seen = seen
    return holder


def _full_booking_script(date_text: str):
    def script(_system, steps):
        function_results = [s for s in steps if s.get("type") == "function_result"]
        if not function_results:
            return _calls(
                (
                    "update_reservation",
                    {
                        "event_date": date_text,
                        "start_time": "20:00",
                        "event_type": "Cumpleaños",
                        "location_address": "Jr. Los Pinos 123",
                        "district": "Lima",
                    },
                )
            )
        if _result_of(steps, "create_booking_link") is None:
            return _calls(("check_availability", {}), ("quote", {}), ("create_booking_link", {}))
        link = _result_of(steps, "create_booking_link")
        return _text(f"¡Listo! Tu show estándar cuesta *S/ {link['total']:.2f}*. Asegura tu fecha aquí: {link['enlace']}")

    return script


def test_full_booking_flow_with_tools(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(_full_booking_script(EVENT_DATE.isoformat()))

    turn = _say(client, "Quiero una serenata de cumpleaños, el día y lugar los tengo claros")

    assert turn["booking_link_url"] and turn["booking_link_url"] in turn["reply"]
    assert "S/ 450.00" in turn["reply"]
    assert turn["status"] == "link_sent"
    link = db_session.query(BookingLink).one()
    assert float(link.price) == 450.0 and link.event_date == EVENT_DATE and link.location_city == "Lima"
    last = turn["messages"][-1]
    assert last["trace"]["engine"] == "agent"
    assert last["trace"]["tools"] == ["update_reservation", "check_availability", "quote", "create_booking_link"]
    assert last["trace"]["rounds"] == 3
    # El turno cuenta como una respuesta del músico; las vueltas extra solo cuentan en el tope global.
    usage = get_user_usage(db_session, profile.user_id)
    assert usage.ai_calls_used == 1
    assert get_global_calls_today(db_session) == 3


def test_the_model_receives_the_conversation_history_and_state(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(lambda _s, _steps: _text("¡Hola! Cuéntame qué celebras y para cuándo."))
    first = _say(client, "Hola")

    engine.use(lambda _s, _steps: _text("Perfecto, ¿y en qué distrito será?"))
    _say(client, "Es un cumpleaños", first["conversation_id"])

    second_call = engine.seen[-1]["steps"]
    assert second_call[0] == {"type": "user_input", "content": "Hola"}
    assert second_call[1]["type"] == "model_output"
    assert "Cuéntame qué celebras" in second_call[1]["content"][0]["text"]
    assert second_call[-1] == {"type": "user_input", "content": "Es un cumpleaños"}


def test_weekday_that_does_not_match_the_date_is_not_saved(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)

    def script(_system, steps):
        result = _result_of(steps, "update_reservation")
        if result is None:
            return _calls(
                ("update_reservation", {"event_date": EVENT_DATE.isoformat(), "weekday_mentioned": WRONG_WEEKDAY})
            )
        assert result["problems"] and "pregúntale" in result["problems"][0]
        return _text("Me dices una fecha que no coincide con ese día. ¿Cuál prefieres?")

    engine.use(script)
    turn = _say(client, f"Es el {WRONG_WEEKDAY} {EVENT_DATE.day} de {_MONTH_NAMES[EVENT_DATE.month - 1]}")

    assert "event_date" not in turn["extracted_data"]
    assert turn["booking_link_url"] is None


def test_out_of_coverage_is_escalated_even_if_the_model_forgets(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)

    def script(_system, steps):
        if _result_of(steps, "quote") is None:
            return _calls(
                ("update_reservation", {"district": "Arequipa", "location_address": "Plaza de Armas de Arequipa"}),
                ("quote", {}),
            )
        return _text("Ese evento queda fuera de nuestras zonas habituales; el músico te confirmará el traslado.")

    engine.use(script)
    turn = _say(client, "Es en Arequipa")

    assert turn["status"] == "needs_musician"
    assert "zonas de cobertura" in turn["escalation_reason"]
    assert db_session.query(Notification).filter(Notification.type == "assistant_escalation").count() == 1


def test_a_price_the_tools_did_not_return_is_rejected_and_the_rules_engine_answers(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(lambda _s, _steps: _text("Te lo dejo en S/ 199.00 y listo."))

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert "199" not in turn["reply"]
    assert "S/ 450.00" in turn["reply"]  # el motor por reglas cotiza con las reglas del músico
    assert "trace" not in turn["messages"][-1] or turn["messages"][-1]["trace"] is None


def test_a_link_the_system_did_not_create_is_rejected(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(lambda _s, _steps: _text("Paga aquí: https://pagos-falsos.example.com/abc"))

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert "pagos-falsos" not in turn["reply"]


def test_without_quota_the_rules_engine_answers_and_the_model_is_not_called(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    row = get_or_create_settings_row(db_session)
    row.monthly_ai_calls_per_musician = 0
    db_session.commit()
    engine.use(lambda _s, _steps: pytest.fail("no debió llamar al modelo sin cupo"))

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert "S/ 450.00" in turn["reply"]
    assert engine.seen == []


def test_when_the_model_fails_the_rules_engine_answers(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(lambda _s, _steps: None)

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert "S/ 450.00" in turn["reply"]


def test_the_default_engine_never_calls_the_agent(client, db_session, monkeypatch):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(assistant_svc, "_call_gemini_interaction", lambda **_kw: None)
    monkeypatch.setattr(agent, "_interactions_call", lambda *_a, **_kw: pytest.fail("el motor por defecto es «rules»"))

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert turn["reply"]


def test_too_many_rounds_fall_back_to_the_rules_engine(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(lambda _s, _steps: _calls(("quote", {})))  # nunca termina de responder

    turn = _say(client, "Hola, ¿cuánto cobran?")

    assert "S/ 450.00" in turn["reply"]
    assert len(engine.seen) == agent.MAX_ROUNDS


def test_a_tool_that_raises_does_not_break_the_turn(client, db_session, engine, monkeypatch):
    profile = _musician(db_session)
    _login(client, db_session, profile)

    def boom(*_a, **_kw):
        raise RuntimeError("falla simulada")

    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "quote", boom)

    def script(_system, steps):
        result = _result_of(steps, "quote")
        if result is None:
            return _calls(("quote", {}))
        assert "error" in result
        return _text("Déjame confirmarlo con el músico y te escribo.")

    engine.use(script)
    turn = _say(client, "¿Cuánto cobran?")

    assert "confirmarlo" in turn["reply"]


def test_conversation_row_keeps_the_reservation_state(client, db_session, engine):
    profile = _musician(db_session)
    _login(client, db_session, profile)
    engine.use(_full_booking_script(EVENT_DATE.isoformat()))

    turn = _say(client, "Quiero reservar")

    conversation = db_session.get(AssistantConversation, turn["conversation_id"])
    assert conversation.extracted_data["event_date"] == EVENT_DATE.isoformat()
    assert conversation.extracted_data["location_city"] == "Lima"
    assert conversation.extracted_data["calculated_price"] == 450.0
    assert db_session.execute(text("select count(*) from booking_link")).scalar() == 1
