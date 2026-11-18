"""WhatsApp del asistente: el filtro de integrantes viene desactivado y los fallos de envío dejan rastro."""

import logging
import uuid
from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.endpoints import assistant as assistant_endpoints
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.musician_assistant import get_or_create_whatsapp_channel, should_ignore_incoming_message

pytestmark = pytest.mark.db

MEMBER_PHONE = "51987654321"


def _musician_with_member(db: Session) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"wa_{suffix}@test.com",
        fullname="Lider",
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
        EnsembleMember(
            leader_user_id=user.id,
            profile_id=profile.id,
            email=f"m_{suffix}@test.com",
            fullname="Integrante Uno",
            phone=MEMBER_PHONE,
            specialties=["Voz"],
            status=EnsembleMemberStatus.active,
        )
    )
    db.commit()
    return user


def test_canal_nuevo_no_filtra_integrantes_por_defecto_pero_si_chats_personales(db_session: Session):
    user = _musician_with_member(db_session)
    channel = get_or_create_whatsapp_channel(db_session, user)
    assert channel.ignore_ensemble_members is False
    assert channel.ignore_personal_chats is True


def test_integrante_recibe_respuesta_con_el_flag_por_defecto(db_session: Session):
    user = _musician_with_member(db_session)
    channel = get_or_create_whatsapp_channel(db_session, user)
    ignore, reason = should_ignore_incoming_message(
        db_session,
        user=user,
        channel=channel,
        client_phone=MEMBER_PHONE,
        message_text="Quiero cotizar un cumpleaños",
        is_new_conversation=True,
    )
    assert ignore is False
    assert reason is None


def test_integrante_se_ignora_si_el_musico_activa_el_flag(db_session: Session):
    user = _musician_with_member(db_session)
    channel = get_or_create_whatsapp_channel(db_session, user)
    channel.ignore_ensemble_members = True
    db_session.commit()
    ignore, reason = should_ignore_incoming_message(
        db_session,
        user=user,
        channel=channel,
        client_phone=MEMBER_PHONE,
        message_text="Quiero cotizar un cumpleaños",
        is_new_conversation=True,
    )
    assert ignore is True
    assert reason and "integrante" in reason.lower()


class _FakeResponse:
    status_code = 401
    text = '{"error":{"message":"Invalid OAuth access token","code":190}}'


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return _FakeResponse()


def test_envio_rechazado_por_meta_deja_rastro_en_el_log(monkeypatch, caplog):
    monkeypatch.setattr(assistant_endpoints.httpx, "Client", _FakeClient)
    with caplog.at_level(logging.WARNING, logger=assistant_endpoints.logger.name):
        ok = assistant_endpoints._send_whatsapp_cloud_message(
            phone_number_id="123", access_token="EAAB-token", to_phone="51999111222", text="Hola"
        )
    assert ok is False
    assert "HTTP 401" in caplog.text
    assert "Invalid OAuth access token" in caplog.text
    # El token nunca se escribe en el log.
    assert "EAAB-token" not in caplog.text
