import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class MusicianAssistantConfig(Base):
    __tablename__ = "musician_assistant_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    greeting_message = Column(String(500), nullable=True)

    # Paquetes por cantidad de integrantes y duración:
    # [{"id": str, "name": str, "members_count": int, "duration_hours": float, "price": float, "description": str | None}]
    packages = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    additional_hour_price = Column(Numeric(10, 2), nullable=True)

    # Recargos por zona/distrito o distancia:
    # [{"zone": str, "surcharge": float, "distance_km": float | None}]
    zone_surcharges = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    per_km_rate = Column(Numeric(10, 2), nullable=True)

    # Recargos por horario nocturno / fechas festivas
    night_surcharge_amount = Column(Numeric(10, 2), nullable=True, default=0)
    night_surcharge_start_hour = Column(Integer, nullable=False, default=23, server_default=text("23"))
    special_date_surcharge_percent = Column(Numeric(5, 2), nullable=True, default=0)

    # Negociación / regateo máximo permitido por el asistente
    max_discount_amount = Column(Numeric(10, 2), nullable=True, default=0)
    max_discount_percent = Column(Numeric(5, 2), nullable=True, default=0)

    # Instrucciones libres en lenguaje natural ("Entrena a tu asistente")
    custom_instructions = Column(Text, nullable=True)

    # Cómo habla: tono ("cercano" | "formal" | "breve") y mensajes de ejemplo del músico para imitar su estilo.
    tone = Column(String(16), nullable=False, default="cercano", server_default=text("'cercano'"))
    voice_examples = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    # Cuándo el asistente deriva al músico: {"big_event_on": bool, "big_event_guests": int,
    # "special_date_on": bool, "unknown_question_on": bool}. Vacío = comportamiento de siempre.
    escalation_rules = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    profile = relationship("MusicianProfile")


class MusicianWhatsAppChannel(Base):
    __tablename__ = "musician_whatsapp_channel"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    phone_number_id = Column(String(64), unique=True, index=True, nullable=True)
    waba_id = Column(String(64), nullable=True)
    display_phone_number = Column(String(40), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    webhook_verify_token = Column(String(128), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    auto_pause_on_human_reply = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    ignore_ensemble_members = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    ignore_personal_chats = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    ignored_phones = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    connected_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")


class AssistantUsageCounter(Base):
    """Contador atómico de uso del asistente.

    La clave codifica qué se cuenta y en qué periodo, por ejemplo `ai:user:<uuid>:2026-11`,
    `audio:user:<uuid>:2026-11` o `ai:global:2026-11-16`. También guarda `wa_msg:<id>` para no procesar
    dos veces un mensaje de WhatsApp que Meta reenvía.
    """

    __tablename__ = "assistant_usage_counter"

    key = Column(String(120), primary_key=True)
    count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AssistantPlatformSettings(Base):
    """Fila única con los topes de uso del asistente, que el admin ajusta sin desplegar."""

    __tablename__ = "assistant_platform_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Llamadas al modelo por músico y mes (cada respuesta o extracción es una llamada). 0 = IA apagada.
    monthly_ai_calls_per_musician = Column(Integer, nullable=False, default=300, server_default=text("300"))
    # Audios transcritos por músico y mes (cada audio cuenta también como llamada al modelo).
    monthly_audio_per_musician = Column(Integer, nullable=False, default=60, server_default=text("60"))
    # Freno de emergencia: llamadas al modelo de toda la plataforma por día.
    daily_ai_calls_global = Column(Integer, nullable=False, default=3000, server_default=text("3000"))
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(UTCDateTime, default=datetime.utcnow)


class AssistantConversation(Base):
    __tablename__ = "assistant_conversation"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # "whatsapp" | "simulator"
    channel = Column(String(24), nullable=False, default="whatsapp", server_default=text("'whatsapp'"))
    client_phone = Column(String(40), nullable=True, index=True)
    client_name = Column(String(120), nullable=True)

    # "bot_active" | "paused_by_musician" | "needs_musician" | "link_sent" | "ignored_personal"
    status = Column(String(32), nullable=False, default="bot_active", server_default=text("'bot_active'"))
    escalation_reason = Column(String(255), nullable=True)

    extracted_data = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    messages = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    last_booking_link_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking_link.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_interaction_id = Column(String(128), nullable=True)
    last_message_at = Column(UTCDateTime, default=datetime.utcnow)
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    profile = relationship("MusicianProfile")
    last_booking_link = relationship("BookingLink")


class AssistantKnowledgeEntry(Base):
    """Pregunta y respuesta que el músico le enseña a su asistente (se usa como dato, no como pregunta exacta)."""

    __tablename__ = "assistant_knowledge_entry"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question = Column(String(300), nullable=False)
    answer = Column(Text, nullable=False)
    # "tu" (escrita por el músico) | "conversacion" (de una duda pendiente) | "entrevista"
    source = Column(String(24), nullable=False, default="tu", server_default=text("'tu'"))
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AssistantPendingQuestion(Base):
    """Duda de un cliente que el asistente no supo responder; el músico la responde una vez y queda enseñada."""

    __tablename__ = "assistant_pending_question"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("assistant_conversation.id", ondelete="SET NULL"),
        nullable=True,
    )
    question = Column(String(300), nullable=False)
    context = Column(Text, nullable=True)
    asked_count = Column(Integer, nullable=False, default=1, server_default=text("1"))
    # "open" | "answered" | "dismissed"
    status = Column(String(16), nullable=False, default="open", server_default=text("'open'"))
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    answered_at = Column(UTCDateTime, nullable=True)


class AssistantConfigVersion(Base):
    """Copia de la configuración de entrenamiento cada vez que cambia, para poder volver a una versión anterior."""

    __tablename__ = "assistant_config_version"
    __table_args__ = (UniqueConstraint("profile_id", "number", name="uq_assistant_config_version_profile_number"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    number = Column(Integer, nullable=False)
    label = Column(String(255), nullable=False, default="", server_default=text("''"))
    snapshot = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at = Column(UTCDateTime, default=datetime.utcnow)

