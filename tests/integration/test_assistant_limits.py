"""Asistente IA: webhook de WhatsApp con firma obligatoria, topes de uso del modelo y límites de los endpoints."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.musician_assistant as assistant_svc
from app.api.v1.endpoints import assistant as assistant_endpoints
from app.core.config import settings
from app.core.jwt import create_access_token
from app.models.musician_assistant import AssistantConversation, MusicianWhatsAppChannel
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.assistant_usage import (
    AssistantQuotaExceeded,
    claim_whatsapp_message,
    consume_ai_call,
    get_global_calls_today,
    get_limits,
    get_or_create_settings_row,
    get_user_usage,
)
from app.services.uniqueness import normalize_phone

pytestmark = pytest.mark.db

APP_SECRET = "test-meta-app-secret"
PHONE_NUMBER_ID = "109876543210"
WEBHOOK = "/api/v1/whatsapp/webhook"


def _user(db: Session, role: UserRole = UserRole.musician) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"u_{suffix}@test.com",
        fullname="Carlos",
        role=role,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    return user


def _musician(db: Session) -> tuple[User, MusicianProfile]:
    user = _user(db)
    suffix = uuid.uuid4().hex[:8]
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=f"Los Test {suffix}",
        slug=f"los-test-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city="Lima",
    )
    db.add(profile)
    db.commit()
    return user, profile


def _login(client: TestClient, user: User) -> None:
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(subject=str(user.id), role=user.role.value, version=user.token_version or 0),
    )


def _set_limits(db: Session, *, ai: int = 300, audio: int = 60, global_daily: int = 3000) -> None:
    row = get_or_create_settings_row(db)
    row.monthly_ai_calls_per_musician = ai
    row.monthly_audio_per_musician = audio
    row.daily_ai_calls_global = global_daily
    db.commit()


def _connect_channel(db: Session, user: User) -> None:
    db.add(
        MusicianWhatsAppChannel(
            user_id=user.id,
            phone_number_id=PHONE_NUMBER_ID,
            access_token_encrypted=assistant_svc.encrypt_whatsapp_token("EAAB-token"),
            is_active=True,
        )
    )
    db.commit()


def _payload(*messages: dict) -> bytes:
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": "51999111222", "profile": {"name": "Ana"}}],
                            "messages": list(messages),
                        }
                    }
                ]
            }
        ]
    }
    return json.dumps(body).encode("utf-8")


def _text_message(message_id: str, body: str = "Hola, ¿cuánto cobran por una serenata de cumpleaños?") -> dict:
    return {"from": "51999111222", "id": message_id, "type": "text", "text": {"body": body}}


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post_webhook(client: TestClient, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(WEBHOOK, content=body, headers=headers)


@pytest.fixture
def sent_messages(monkeypatch):
    """Registra lo que el asistente enviaría por WhatsApp en vez de llamar a Meta."""
    sent: list[dict] = []

    def fake_send(**kwargs) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(assistant_endpoints, "_send_whatsapp_cloud_message", fake_send)
    return sent


@pytest.fixture
def gemini_calls(monkeypatch):
    """Con API key configurada, cuenta las llamadas HTTP al modelo sin salir a internet."""
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "json": json})
            return SimpleNamespace(status_code=200, text="", json=lambda: {"output_text": "Respuesta del modelo"})

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(assistant_svc.httpx, "Client", FakeClient)
    return calls


# --- Webhook: la firma es obligatoria ---


def test_webhook_rejects_everything_when_app_secret_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "META_WHATSAPP_APP_SECRET", "")
    body = _payload(_text_message("wamid.1"))

    res = _post_webhook(client, body, _sign(body, ""))

    assert res.status_code == 503


def test_webhook_rejects_missing_or_wrong_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "META_WHATSAPP_APP_SECRET", APP_SECRET)
    body = _payload(_text_message("wamid.1"))

    assert _post_webhook(client, body, None).status_code == 401
    assert _post_webhook(client, body, _sign(body, "otro-secreto")).status_code == 401
    # Una firma válida de otro cuerpo tampoco sirve.
    assert _post_webhook(client, body, _sign(b"{}")).status_code == 401


def test_webhook_with_valid_signature_replies_once_and_ignores_resent_message(
    client, db_session, monkeypatch, sent_messages
):
    monkeypatch.setattr(settings, "META_WHATSAPP_APP_SECRET", APP_SECRET)
    user, _profile = _musician(db_session)
    _connect_channel(db_session, user)
    body = _payload(_text_message("wamid.dup"))

    first = _post_webhook(client, body, _sign(body))
    again = _post_webhook(client, body, _sign(body))

    assert first.status_code == 200
    assert [r["type"] for r in first.json()["processed"]] == ["message_processed"]
    assert again.status_code == 200
    assert again.json()["processed"] == []
    # Se contestó una sola vez, y solo como respuesta al mensaje del cliente.
    assert len(sent_messages) == 1
    assert sent_messages[0]["to_phone"] == "51999111222"
    conv = db_session.query(AssistantConversation).filter_by(user_id=user.id).one()
    assert [m["sender"] for m in conv.messages] == ["client", "assistant"]


def test_assistant_never_sends_templates_or_writes_first():
    """El envío solo sabe mandar texto libre; no existe ruta de plantillas ni de inicio de conversación."""
    import inspect

    source = inspect.getsource(assistant_endpoints._send_whatsapp_cloud_message)
    assert '"type": "text"' in source
    assert "template" not in source.lower()


# --- Topes de uso ---


def test_user_monthly_cap_blocks_after_limit(db_session):
    user = _user(db_session)
    _set_limits(db_session, ai=2)

    assert consume_ai_call(db_session, user.id) is True
    assert consume_ai_call(db_session, user.id) is True
    assert consume_ai_call(db_session, user.id) is False
    usage = get_user_usage(db_session, user.id)
    assert (usage.ai_calls_used, usage.ai_calls_limit) == (2, 2)


def test_audio_has_its_own_cap_and_zero_disables(db_session):
    user = _user(db_session)
    _set_limits(db_session, ai=10, audio=1)

    assert consume_ai_call(db_session, user.id, audio=True) is True
    assert consume_ai_call(db_session, user.id, audio=True) is False
    # El texto sigue disponible aunque se acabaron los audios.
    assert consume_ai_call(db_session, user.id) is True

    _set_limits(db_session, ai=0)
    assert consume_ai_call(db_session, user.id) is False


def test_global_daily_cap_applies_across_musicians(db_session):
    first, second = _user(db_session), _user(db_session)
    _set_limits(db_session, ai=10, global_daily=2)

    assert consume_ai_call(db_session, first.id) is True
    assert consume_ai_call(db_session, second.id) is True
    assert consume_ai_call(db_session, first.id) is False
    assert get_global_calls_today(db_session) == 2


def test_defaults_apply_when_admin_never_saved_limits(db_session):
    limits = get_limits(db_session)

    assert (limits.monthly_ai_calls_per_musician, limits.monthly_audio_per_musician, limits.daily_ai_calls_global) == (
        300,
        60,
        3000,
    )


def test_whatsapp_message_is_claimed_only_once(db_session):
    assert claim_whatsapp_message(db_session, "wamid.A") is True
    assert claim_whatsapp_message(db_session, "wamid.A") is False
    assert claim_whatsapp_message(db_session, "wamid.B") is True


def test_model_is_not_called_without_quota_context(monkeypatch, gemini_calls):
    # Sin contexto de cupo no se llama al modelo, aunque haya API key.
    assert assistant_svc._call_gemini_interaction(system_instruction="x", user_content="hola") is None
    assert gemini_calls == []


def test_model_call_is_refused_once_quota_is_spent(db_session, gemini_calls):
    user = _user(db_session)
    _set_limits(db_session, ai=1)

    first = assistant_svc._call_gemini_interaction(
        system_instruction="x", user_content="hola", usage_db=db_session, usage_user_id=user.id
    )
    assert first and first["output_text"] == "Respuesta del modelo"

    with pytest.raises(AssistantQuotaExceeded):
        assistant_svc._call_gemini_interaction(
            system_instruction="x", user_content="hola", usage_db=db_session, usage_user_id=user.id
        )
    assert len(gemini_calls) == 1


def test_text_turn_keeps_answering_by_rules_when_quota_is_spent(client, db_session, gemini_calls):
    user, _profile = _musician(db_session)
    _set_limits(db_session, ai=0)
    _login(client, user)

    res = client.post("/api/v1/assistant/simulate", json={"message": "Hola, ¿cuánto cobran por una serenata?"})

    assert res.status_code == 200, res.text
    assert res.json()["reply"]
    assert gemini_calls == []


def test_audio_turn_is_refused_with_429_when_audio_quota_is_spent(client, db_session, gemini_calls):
    user, _profile = _musician(db_session)
    _set_limits(db_session, audio=0)
    _login(client, user)

    res = client.post("/api/v1/assistant/simulate", json={"audio_base64": "QUJD", "audio_mime_type": "audio/webm"})

    assert res.status_code == 429
    assert gemini_calls == []


def test_autocomplete_refuses_audio_without_quota_but_accepts_text(client, db_session, gemini_calls):
    user, _profile = _musician(db_session)
    _set_limits(db_session, ai=0, audio=0)
    _login(client, user)

    audio = client.post("/api/v1/booking-links/autocomplete", json={"audio_base64": "QUJD"})
    text = client.post("/api/v1/booking-links/autocomplete", json={"text": "Serenata el 2026-12-20 a las 9pm"})

    assert audio.status_code == 429
    assert text.status_code == 200
    assert text.json()["event_date"] == "2026-12-20"
    assert gemini_calls == []


def test_whatsapp_audio_without_quota_asks_the_client_to_write(client, db_session, monkeypatch, sent_messages, gemini_calls):
    monkeypatch.setattr(settings, "META_WHATSAPP_APP_SECRET", APP_SECRET)
    user, _profile = _musician(db_session)
    _connect_channel(db_session, user)
    _set_limits(db_session, audio=0)
    monkeypatch.setattr(assistant_endpoints, "_download_whatsapp_audio_base64", lambda *a, **k: ("QUJD", "audio/ogg"))
    body = _payload({"from": "51999111222", "id": "wamid.audio", "type": "audio", "audio": {"id": "media-1"}})

    res = _post_webhook(client, body, _sign(body))

    assert res.status_code == 200
    assert [r["type"] for r in res.json()["processed"]] == ["audio_quota_exceeded"]
    assert [m["text"] for m in sent_messages] == [assistant_endpoints.AUDIO_UNAVAILABLE_REPLY]
    assert gemini_calls == []


def test_audio_from_a_paused_chat_is_not_transcribed(db_session, monkeypatch):
    user, profile = _musician(db_session)
    _connect_channel(db_session, user)
    db_session.add(
        AssistantConversation(
            user_id=user.id,
            profile_id=profile.id,
            channel="whatsapp",
            # El asistente guarda los teléfonos normalizados.
            client_phone=normalize_phone("51999111222"),
            status="paused_by_musician",
        )
    )
    db_session.commit()
    transcribed: list[bool] = []
    monkeypatch.setattr(
        assistant_svc,
        "extract_booking_fields_from_text_or_audio",
        lambda **kwargs: transcribed.append(True) or {},
    )

    assistant_svc.process_assistant_turn(
        db_session,
        user=user,
        channel_type="whatsapp",
        audio_base64="QUJD",
        client_phone="51999111222",
        client_name="Ana",
    )

    assert transcribed == []


# --- Límites de los endpoints ---


def test_simulate_is_rate_limited_per_minute(client, db_session, monkeypatch):
    user, _profile = _musician(db_session)
    _login(client, user)
    statuses = [
        client.post("/api/v1/assistant/simulate", json={"message": f"Hola {i}, ¿cuánto cobran?"}).status_code
        for i in range(22)
    ]

    assert statuses[0] == 200
    assert statuses[-1] == 429


def test_audio_payload_over_the_size_cap_is_rejected(client, db_session):
    user, _profile = _musician(db_session)
    _login(client, user)

    res = client.post("/api/v1/assistant/simulate", json={"audio_base64": "A" * 7_000_001})

    # El límite de tamaño lo corta el middleware (413) o la validación del esquema (422).
    assert res.status_code in (413, 422)


# --- Vista del músico y del admin ---


def test_musician_sees_their_monthly_usage(client, db_session):
    user, _profile = _musician(db_session)
    _set_limits(db_session, ai=50, audio=5)
    consume_ai_call(db_session, user.id, audio=True)
    _login(client, user)

    res = client.get("/api/v1/assistant/usage")

    assert res.status_code == 200
    assert res.json() == {"ai_calls_used": 1, "ai_calls_limit": 50, "audio_used": 1, "audio_limit": 5}


def test_admin_reads_and_updates_limits_and_musicians_cannot(client, db_session):
    admin = _user(db_session, UserRole.admin)
    musician, _profile = _musician(db_session)

    _login(client, musician)
    assert client.get("/api/v1/admin/assistant-limits").status_code == 403
    assert client.put("/api/v1/admin/assistant-limits", json={}).status_code in (403, 422)

    _login(client, admin)
    current = client.get("/api/v1/admin/assistant-limits")
    assert current.status_code == 200
    assert current.json()["monthly_ai_calls_per_musician"] == 300

    updated = client.put(
        "/api/v1/admin/assistant-limits",
        json={"monthly_ai_calls_per_musician": 120, "monthly_audio_per_musician": 15, "daily_ai_calls_global": 900},
    )
    assert updated.status_code == 200
    assert updated.json()["monthly_ai_calls_per_musician"] == 120
    assert get_limits(db_session).daily_ai_calls_global == 900

    invalid = client.put(
        "/api/v1/admin/assistant-limits",
        json={"monthly_ai_calls_per_musician": -1, "monthly_audio_per_musician": 15, "daily_ai_calls_global": 900},
    )
    assert invalid.status_code == 422
