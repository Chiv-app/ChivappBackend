"""Entrenamiento del asistente: conocimiento, dudas pendientes, versiones, preparación, vista previa y reglas de derivación."""

import uuid
from datetime import datetime

import pytest

import app.services.assistant_agent as agent
import app.services.musician_assistant as assistant_svc
from app.core.config import settings
from app.core.jwt import create_access_token
from app.models.musician_assistant import (
    AssistantConfigVersion,
    AssistantKnowledgeEntry,
    AssistantPendingQuestion,
    MusicianAssistantConfig,
)
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.notification import Notification
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services import assistant_training as training
from app.services.assistant_knowledge import build_chunks

pytestmark = pytest.mark.db

PACKAGES = [
    {"id": "p4", "name": "Show estándar", "members_count": 4, "duration_hours": 1.0, "price": 450.0},
    {"id": "p2", "name": "Dúo", "members_count": 2, "duration_hours": 1.0, "price": 280.0},
]


def _musician(db):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"t_{suffix}@test.com",
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
    config = MusicianAssistantConfig(
        profile_id=profile.id,
        enabled=True,
        packages=PACKAGES,
        zone_surcharges=[{"zone": "Lima", "surcharge": 0.0, "distance_km": 0.0}],
        max_discount_percent=10,
    )
    db.add(config)
    db.commit()
    return profile, config


def _login(client, db, profile):
    user = db.get(User, profile.user_id)
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0)
    )


def _config_payload(**overrides):
    payload = {
        "enabled": True,
        "greeting_message": "¡Hola!",
        "packages": PACKAGES,
        "additional_hour_price": 200,
        "zone_surcharges": [{"zone": "Lima", "surcharge": 0}],
        "night_surcharge_amount": 0,
        "night_surcharge_start_hour": 23,
        "special_date_surcharge_percent": 0,
        "max_discount_amount": 0,
        "max_discount_percent": 10,
    }
    payload.update(overrides)
    return payload


# --- Conocimiento ---


def test_knowledge_crud_and_ownership(client, db_session):
    profile, _ = _musician(db_session)
    other, _ = _musician(db_session)
    _login(client, db_session, profile)

    created = client.post("/api/v1/assistant/knowledge", json={"question": "¿Qué incluye el show?", "answer": "Sonido propio y 1 hora."})
    assert created.status_code == 201
    entry_id = created.json()["id"]
    assert [e["question"] for e in client.get("/api/v1/assistant/knowledge").json()] == ["¿Qué incluye el show?"]

    updated = client.put(f"/api/v1/assistant/knowledge/{entry_id}", json={"question": "¿Qué incluye el show?", "answer": "Sonido, luces y 1 hora."})
    assert updated.status_code == 200 and "luces" in updated.json()["answer"]

    _login(client, db_session, other)
    assert client.put(f"/api/v1/assistant/knowledge/{entry_id}", json={"question": "Otra pregunta", "answer": "Otra respuesta"}).status_code == 404
    assert client.delete(f"/api/v1/assistant/knowledge/{entry_id}").status_code == 404

    _login(client, db_session, profile)
    assert client.delete(f"/api/v1/assistant/knowledge/{entry_id}").status_code == 204
    assert client.get("/api/v1/assistant/knowledge").json() == []


@pytest.mark.parametrize("question", ["¿Piden anticipo?", "¿Qué pasa si cancelo?", "¿A qué hora llegan?", "¿Hacen reembolso?"])
def test_the_musician_cannot_teach_what_the_contract_defines(client, db_session, question):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    res = client.post("/api/v1/assistant/knowledge", json={"question": question, "answer": "Sí, con 50 % de adelanto."})

    assert res.status_code == 422 and "contrata" in res.json()["detail"]
    assert db_session.query(AssistantKnowledgeEntry).count() == 0


def test_taught_answers_become_knowledge_chunks():
    profile = type("P", (), {"id": uuid.uuid4(), "contract_template_body": None, "bio": None, "genres": [], "repertoire": [], "songs": [], "details": {}, "showreel_video_url": None, "videos": [], "portfolio_description": None})()
    config = type("C", (), {"packages": [], "custom_instructions": None})()
    entry = AssistantKnowledgeEntry(id=uuid.uuid4(), question="¿Tocan en matrimonios?", answer="Sí, civiles y religiosos.")

    chunks = build_chunks(profile, config, [entry])

    assert chunks[0].source == "faq" and "matrimonios" in chunks[0].content and "religiosos" in chunks[0].content


# --- Dudas pendientes ---


def test_pending_question_is_answered_into_knowledge(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    pending = training.register_pending_question(db_session, profile, None, "¿Pueden tocar en la playa?", "Queremos algo en la playa")
    again = training.register_pending_question(db_session, profile, None, "¿pueden tocar en la playa?", None)
    db_session.commit()
    assert again.id == pending.id and again.asked_count == 2

    listed = client.get("/api/v1/assistant/pending").json()
    assert [p["question"] for p in listed] == ["¿Pueden tocar en la playa?"] and listed[0]["asked_count"] == 2

    answered = client.post(f"/api/v1/assistant/pending/{pending.id}/answer", json={"answer": "Sí, con sonido propio."})
    assert answered.status_code == 201 and answered.json()["source"] == "conversacion"
    assert client.get("/api/v1/assistant/pending").json() == []
    assert db_session.get(AssistantPendingQuestion, pending.id).status == "answered"
    assert client.post(f"/api/v1/assistant/pending/{pending.id}/answer", json={"answer": "Otra vez"}).status_code == 404


def test_pending_question_can_be_dismissed(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    pending = training.register_pending_question(db_session, profile, None, "¿Tocan gratis?", None)
    db_session.commit()

    assert client.post(f"/api/v1/assistant/pending/{pending.id}/dismiss").status_code == 204
    assert client.get("/api/v1/assistant/pending").json() == []
    assert db_session.query(AssistantKnowledgeEntry).count() == 0


# --- Configuración: tono, voz, reglas y versiones ---


def test_config_saves_tone_voice_and_rules_and_old_payloads_do_not_reset_them(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    saved = client.put(
        "/api/v1/assistant/config",
        json=_config_payload(
            tone="formal",
            voice_examples=["Con gusto, ya te cuento.", " ", "Perfecto, lo anoto."],
            escalation_rules={"big_event_on": True, "big_event_guests": 80, "special_date_on": True, "unknown_question_on": False},
        ),
    ).json()
    assert saved["tone"] == "formal"
    assert saved["voice_examples"] == ["Con gusto, ya te cuento.", "Perfecto, lo anoto."]
    assert saved["escalation_rules"] == {"big_event_on": True, "big_event_guests": 80, "special_date_on": True, "unknown_question_on": False}

    # La pantalla anterior no envía los campos nuevos: no deben volver a sus valores por defecto.
    legacy = client.put("/api/v1/assistant/config", json=_config_payload(greeting_message="Hola de nuevo")).json()
    assert legacy["tone"] == "formal" and legacy["voice_examples"] and legacy["escalation_rules"]["big_event_on"] is True


def test_new_config_defaults_reproduce_todays_behaviour(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    config = client.get("/api/v1/assistant/config").json()

    assert config["tone"] == "cercano" and config["voice_examples"] == []
    assert config["escalation_rules"] == {"big_event_on": False, "big_event_guests": 100, "special_date_on": False, "unknown_question_on": True}


def test_versions_are_recorded_only_when_something_changes_and_can_be_restored(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    client.put("/api/v1/assistant/config", json=_config_payload())
    client.put("/api/v1/assistant/config", json=_config_payload())  # sin cambios: no crea versión
    client.put("/api/v1/assistant/config", json=_config_payload(max_discount_percent=20))
    versions = client.get("/api/v1/assistant/versions").json()

    assert [v["number"] for v in versions] == [2, 1]
    assert "descuento en porcentaje" in versions[0]["label"]
    assert versions[0]["is_current"] is True and versions[1]["is_current"] is False

    restored = client.post("/api/v1/assistant/versions/1/restore")
    assert restored.status_code == 200 and "Restauraste la versión 1" in restored.json()["label"]
    assert client.get("/api/v1/assistant/config").json()["max_discount_percent"] == 10
    assert [v["number"] for v in client.get("/api/v1/assistant/versions").json()] == [3, 2, 1]
    assert client.post("/api/v1/assistant/versions/99/restore").status_code == 404
    assert db_session.query(AssistantConfigVersion).count() == 3


# --- Preparación y vista previa ---


def test_readiness_improves_as_the_musician_teaches_the_assistant(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    before = client.get("/api/v1/assistant/readiness").json()

    for question in ("¿Qué incluye el show?", "¿Qué música tocan?", "¿Tocan en matrimonios?"):
        client.post("/api/v1/assistant/knowledge", json={"question": question, "answer": "Sí, claro que sí."})
    client.put("/api/v1/assistant/config", json=_config_payload(voice_examples=["a b c", "d e f", "g h i"]))
    after = client.get("/api/v1/assistant/readiness").json()

    assert after["score"] > before["score"]
    knowledge = {i["key"]: i for i in after["items"]}["knowledge"]
    assert knowledge["progress"] == 0.5
    assert {i["key"] for i in after["items"]} == {"pricing", "knowledge", "voice", "pending", "whatsapp", "tests"}


def test_preview_quote_uses_the_unsaved_rules_without_saving_them(client, db_session):
    profile, config = _musician(db_session)
    _login(client, db_session, profile)

    saved = client.post("/api/v1/assistant/preview/quote", json={}).json()
    edited = client.post(
        "/api/v1/assistant/preview/quote",
        json={"config": _config_payload(packages=[{**PACKAGES[0], "price": 500.0}, PACKAGES[1]])},
    ).json()

    assert saved["total"] == 450.0 and edited["total"] == 500.0
    assert edited["lines"][0] == {"label": "Show estándar", "amount": 500.0}
    db_session.refresh(config)
    assert config.packages[0]["price"] == 450.0


def test_preview_compare_marks_only_the_scenarios_that_change(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    compare = client.post("/api/v1/assistant/preview/compare", json={"config": _config_payload(max_discount_percent=20)}).json()

    by_key = {s["key"]: s for s in compare}
    assert by_key["discount"]["changed"] is True and by_key["discount"]["proposed_total"] < by_key["discount"]["current_total"]
    assert by_key["standard"]["changed"] is False
    assert "cheaper" in by_key


# --- Reglas de derivación en el motor por reglas ---

SIMULATE = "/api/v1/assistant/simulate"


def _say(client, text, conversation_id=None):
    res = client.post(SIMULATE, json={"message": text, "conversation_id": conversation_id})
    assert res.status_code == 200, res.text
    return res.json()


def test_a_taught_answer_is_used_by_the_rules_engine(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    client.post("/api/v1/assistant/knowledge", json={"question": "¿Tocan en matrimonios?", "answer": "Sí, civiles y religiosos."})

    turn = _say(client, "Hola, ¿ustedes tocan en matrimonios?")

    assert "civiles y religiosos" in turn["reply"]


def test_big_events_are_handed_to_the_musician_only_when_the_rule_is_on(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    message = "Es una boda con 150 invitados el 5 de diciembre a las 8 pm en Jr. Pinos 123, Lima"

    default = _say(client, message)
    assert default["status"] != "needs_musician"

    client.put("/api/v1/assistant/config", json=_config_payload(escalation_rules={"big_event_on": True, "big_event_guests": 100}))
    escalated = _say(client, message)

    assert escalated["status"] == "needs_musician" and "150 personas" in escalated["escalation_reason"]
    assert "coordina el músico" in escalated["reply"] and escalated["booking_link_url"] is None
    assert db_session.query(Notification).filter(Notification.type == "assistant_escalation").count() == 1


def test_special_dates_are_handed_to_the_musician_when_the_rule_is_on(client, db_session):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    client.put("/api/v1/assistant/config", json=_config_payload(escalation_rules={"special_date_on": True}))

    turn = _say(client, "Quiero una serenata de cumpleaños el 25 de diciembre a las 8 pm en Jr. Pinos 123, Lima")

    assert turn["status"] == "needs_musician" and "Navidad" in turn["escalation_reason"]


# --- Reglas de derivación y dudas en el motor con herramientas ---


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_ENGINE", "agent")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(assistant_svc, "_call_gemini_interaction", lambda **_kw: None)

    class Script:
        script = None

    holder = Script()
    holder.seen = []

    def fake(system_prompt, steps):
        holder.seen.append({"system": system_prompt, "steps": steps})
        return holder.script(system_prompt, steps)

    monkeypatch.setattr(agent, "_interactions_call", fake)
    return holder


def _calls(*calls):
    return {
        "status": "requires_action",
        "steps": [{"type": "function_call", "id": f"call_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(calls)],
    }


def _text(value):
    return {"status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": value}]}]}


def test_agent_registers_an_unknown_question_for_the_musician(client, db_session, engine):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)

    def script(_system, steps):
        if not any(s.get("type") == "function_result" for s in steps):
            return _calls(("ask_musician", {"question": "¿Pueden tocar en la playa?"}))
        return _text("Eso te lo confirma el músico por aquí.")

    engine.script = script
    turn = _say(client, "¿Pueden tocar en la playa?")

    assert "el músico" in turn["reply"]
    pending = db_session.query(AssistantPendingQuestion).one()
    assert pending.question == "¿Pueden tocar en la playa?" and pending.status == "open"
    assert "Estilo:" in engine.seen[0]["system"]


def test_agent_does_not_register_questions_when_the_musician_turned_that_off(client, db_session, engine):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    client.put("/api/v1/assistant/config", json=_config_payload(escalation_rules={"unknown_question_on": False}))

    def script(_system, steps):
        if not any(s.get("type") == "function_result" for s in steps):
            return _calls(("ask_musician", {"question": "¿Tocan en la playa?"}))
        return _text("No tengo ese dato.")

    engine.script = script
    _say(client, "¿Tocan en la playa?")

    assert db_session.query(AssistantPendingQuestion).count() == 0


def test_agent_hands_off_a_big_event_even_if_the_model_forgets(client, db_session, engine):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    client.put("/api/v1/assistant/config", json=_config_payload(escalation_rules={"big_event_on": True, "big_event_guests": 100}))

    def script(_system, steps):
        if not any(s.get("type") == "function_result" for s in steps):
            return _calls(("update_reservation", {"guests_count": 300}))
        return _text("Para un evento tan grande prefiero que el músico te confirme los detalles.")

    engine.script = script
    turn = _say(client, "Es una boda de 300 invitados")

    assert turn["status"] == "needs_musician" and "300 personas" in turn["escalation_reason"]


def test_agent_tone_and_voice_reach_the_prompt(client, db_session, engine):
    profile, _ = _musician(db_session)
    _login(client, db_session, profile)
    client.put("/api/v1/assistant/config", json=_config_payload(tone="formal", voice_examples=["Con gusto, ya te cuento."]))
    engine.script = lambda _s, _steps: _text("Buenas tardes.")

    _say(client, "Hola")

    system = engine.seen[0]["system"]
    assert "Trato de usted" in system and "Con gusto, ya te cuento." in system
