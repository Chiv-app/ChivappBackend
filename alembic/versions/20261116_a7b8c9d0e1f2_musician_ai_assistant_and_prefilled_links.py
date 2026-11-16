"""Asistente IA entrenado por perfil, canal WhatsApp Business y links de contrato pre-llenados

Agrega las columnas de pre-llenado a `booking_link` (para llevar toda la conversación a `/pay/{token}`)
y las tablas `musician_assistant_config`, `musician_whatsapp_channel` y `assistant_conversation`,
más `assistant_usage_counter` y `assistant_platform_settings` (topes de uso del modelo de IA).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS end_time TIME")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS event_type VARCHAR(120)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_address VARCHAR(300)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_city VARCHAR(120)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_reference VARCHAR(255)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_fullname VARCHAR(120)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_phone VARCHAR(40)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_document_number VARCHAR(20)")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS event_description TEXT")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'manual'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS musician_assistant_config (
            id UUID PRIMARY KEY,
            profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            greeting_message VARCHAR(500),
            packages JSONB NOT NULL DEFAULT '[]'::jsonb,
            additional_hour_price NUMERIC(10, 2),
            zone_surcharges JSONB NOT NULL DEFAULT '[]'::jsonb,
            per_km_rate NUMERIC(10, 2),
            night_surcharge_amount NUMERIC(10, 2) DEFAULT 0,
            night_surcharge_start_hour INTEGER NOT NULL DEFAULT 23,
            special_date_surcharge_percent NUMERIC(5, 2) DEFAULT 0,
            max_discount_amount NUMERIC(10, 2) DEFAULT 0,
            max_discount_percent NUMERIC(5, 2) DEFAULT 0,
            custom_instructions TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_assistant_config_profile_id "
        "ON musician_assistant_config (profile_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS musician_whatsapp_channel (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            phone_number_id VARCHAR(64),
            waba_id VARCHAR(64),
            display_phone_number VARCHAR(40),
            access_token_encrypted TEXT,
            webhook_verify_token VARCHAR(128),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            auto_pause_on_human_reply BOOLEAN NOT NULL DEFAULT TRUE,
            ignore_ensemble_members BOOLEAN NOT NULL DEFAULT TRUE,
            ignore_personal_chats BOOLEAN NOT NULL DEFAULT TRUE,
            ignored_phones JSONB NOT NULL DEFAULT '[]'::jsonb,
            connected_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_whatsapp_channel_user_id "
        "ON musician_whatsapp_channel (user_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_whatsapp_channel_phone_number_id "
        "ON musician_whatsapp_channel (phone_number_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_conversation (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            profile_id UUID REFERENCES musician_profile(id) ON DELETE SET NULL,
            channel VARCHAR(24) NOT NULL DEFAULT 'whatsapp',
            client_phone VARCHAR(40),
            client_name VARCHAR(120),
            status VARCHAR(32) NOT NULL DEFAULT 'bot_active',
            escalation_reason VARCHAR(255),
            extracted_data JSONB NOT NULL DEFAULT '{}'::jsonb,
            messages JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_booking_link_id UUID REFERENCES booking_link(id) ON DELETE SET NULL,
            last_interaction_id VARCHAR(128),
            last_message_at TIMESTAMPTZ DEFAULT NOW(),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_user_id "
        "ON assistant_conversation (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_profile_id "
        "ON assistant_conversation (profile_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_client_phone "
        "ON assistant_conversation (client_phone)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_usage_counter (
            key VARCHAR(120) PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_platform_settings (
            id UUID PRIMARY KEY,
            monthly_ai_calls_per_musician INTEGER NOT NULL DEFAULT 300,
            monthly_audio_per_musician INTEGER NOT NULL DEFAULT 60,
            daily_ai_calls_global INTEGER NOT NULL DEFAULT 3000,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
