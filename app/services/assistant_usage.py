"""Topes de uso del modelo de IA del asistente.

Chivapp paga las llamadas al modelo, así que cada una se descuenta de tres cupos antes de hacerla:

- `monthly_audio_per_musician`: audios transcritos por músico y mes (solo si la llamada lleva audio).
- `monthly_ai_calls_per_musician`: llamadas al modelo por músico y mes.
- `daily_ai_calls_global`: freno de emergencia para toda la plataforma por día.

Los valores los ajusta el admin (`assistant_platform_settings`). El contador se incrementa con un único
`INSERT ... ON CONFLICT DO UPDATE ... WHERE count < límite`, así que dos peticiones simultáneas no pasan el tope
juntas. Corre en una sesión propia y confirma enseguida: lo gastado en el modelo no se deshace aunque la
petición falle después. Si un cupo falla, los que ya se descontaron antes en la misma llamada quedan descontados
(sobrecuenta mínima del lado de la seguridad).

Un límite en 0 apaga esa vía por completo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.musician_assistant import AssistantPlatformSettings

logger = logging.getLogger(__name__)

LIMA_TZ = ZoneInfo("America/Lima")

DEFAULT_MONTHLY_AI_CALLS = 300
DEFAULT_MONTHLY_AUDIO = 60
DEFAULT_DAILY_GLOBAL_AI_CALLS = 3000
MAX_LIMIT = 1_000_000

_CONSUME_SQL = text(
    """
    INSERT INTO assistant_usage_counter (key, count, updated_at)
    VALUES (:key, 1, NOW())
    ON CONFLICT (key) DO UPDATE
        SET count = assistant_usage_counter.count + 1, updated_at = NOW()
        WHERE assistant_usage_counter.count < :limit
    RETURNING count
    """
)


class AssistantQuotaExceeded(Exception):
    """No queda cupo para llamar al modelo. `kind` es "audio" o "ai"."""

    def __init__(self, kind: str = "ai") -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class AssistantLimits:
    monthly_ai_calls_per_musician: int
    monthly_audio_per_musician: int
    daily_ai_calls_global: int


@dataclass(frozen=True)
class AssistantUsage:
    ai_calls_used: int
    ai_calls_limit: int
    audio_used: int
    audio_limit: int


def _now() -> datetime:
    return datetime.now(LIMA_TZ)


def _month(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def get_settings_row(db: Session) -> AssistantPlatformSettings | None:
    return db.query(AssistantPlatformSettings).order_by(AssistantPlatformSettings.created_at).first()


def get_or_create_settings_row(db: Session) -> AssistantPlatformSettings:
    row = get_settings_row(db)
    if row is None:
        row = AssistantPlatformSettings(
            monthly_ai_calls_per_musician=DEFAULT_MONTHLY_AI_CALLS,
            monthly_audio_per_musician=DEFAULT_MONTHLY_AUDIO,
            daily_ai_calls_global=DEFAULT_DAILY_GLOBAL_AI_CALLS,
        )
        db.add(row)
        db.flush()
    return row


def get_limits(db: Session) -> AssistantLimits:
    """Topes vigentes. Sin fila guardada rigen los valores por defecto."""
    row = get_settings_row(db)
    if row is None:
        return AssistantLimits(DEFAULT_MONTHLY_AI_CALLS, DEFAULT_MONTHLY_AUDIO, DEFAULT_DAILY_GLOBAL_AI_CALLS)
    return AssistantLimits(
        monthly_ai_calls_per_musician=int(row.monthly_ai_calls_per_musician),
        monthly_audio_per_musician=int(row.monthly_audio_per_musician),
        daily_ai_calls_global=int(row.daily_ai_calls_global),
    )


def _consume(db: Session, key: str, limit: int) -> bool:
    """Descuenta 1 del contador `key` si aún no llegó a `limit`. Devuelve False si el cupo ya está agotado."""
    if limit <= 0:
        return False
    with Session(bind=db.get_bind()) as counter_session:
        row = counter_session.execute(_CONSUME_SQL, {"key": key, "limit": limit}).first()
        counter_session.commit()
    return row is not None


def consume_ai_call(db: Session, user_id: UUID | str, *, audio: bool = False) -> bool:
    """Reserva una llamada al modelo para este músico. False = sin cupo: no se debe llamar al modelo."""
    limits = get_limits(db)
    now = _now()
    month = _month(now)
    if audio and not _consume(db, f"audio:user:{user_id}:{month}", limits.monthly_audio_per_musician):
        return False
    if not _consume(db, f"ai:user:{user_id}:{month}", limits.monthly_ai_calls_per_musician):
        return False
    if not _consume(db, f"ai:global:{_day(now)}", limits.daily_ai_calls_global):
        logger.warning("Tope global diario de llamadas al modelo del asistente alcanzado (%s).", _day(now))
        return False
    return True


def claim_whatsapp_message(db: Session, message_id: str) -> bool:
    """True la primera vez que se ve este mensaje de WhatsApp; False si Meta lo reenvía."""
    clean = (message_id or "").strip()
    if not clean:
        return True
    return _consume(db, f"wa_msg:{clean[:100]}", 1)


def _read_count(db: Session, key: str) -> int:
    value = db.execute(text("SELECT count FROM assistant_usage_counter WHERE key = :key"), {"key": key}).scalar()
    return int(value or 0)


def get_user_usage(db: Session, user_id: UUID | str) -> AssistantUsage:
    limits = get_limits(db)
    month = _month(_now())
    return AssistantUsage(
        ai_calls_used=_read_count(db, f"ai:user:{user_id}:{month}"),
        ai_calls_limit=limits.monthly_ai_calls_per_musician,
        audio_used=_read_count(db, f"audio:user:{user_id}:{month}"),
        audio_limit=limits.monthly_audio_per_musician,
    )


def get_global_calls_today(db: Session) -> int:
    return _read_count(db, f"ai:global:{_day(_now())}")
