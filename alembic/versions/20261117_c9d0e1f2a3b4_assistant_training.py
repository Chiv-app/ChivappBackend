"""Entrenamiento del asistente IA: tono, voz, reglas de derivación, conocimiento, dudas pendientes y versiones

Agrega a `musician_assistant_config` las columnas `tone`, `voice_examples` y `escalation_rules` (con valores por
defecto que reproducen el comportamiento de siempre) y crea `assistant_knowledge_entry`,
`assistant_pending_question` y `assistant_config_version`.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS tone VARCHAR(16) NOT NULL DEFAULT 'cercano'")
    op.execute("ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS voice_examples JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS escalation_rules JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_knowledge_entry (
            id UUID PRIMARY KEY,
            profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
            question VARCHAR(300) NOT NULL,
            answer TEXT NOT NULL,
            source VARCHAR(24) NOT NULL DEFAULT 'tu',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )

        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_entry_profile_id ON assistant_knowledge_entry (profile_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_pending_question (
            id UUID PRIMARY KEY,
            profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
            conversation_id UUID REFERENCES assistant_conversation(id) ON DELETE SET NULL,
            question VARCHAR(300) NOT NULL,
            context TEXT,
            asked_count INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(16) NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            answered_at TIMESTAMPTZ
        )

        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_assistant_pending_question_profile_id ON assistant_pending_question (profile_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_config_version (
            id UUID PRIMARY KEY,
            profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
            number INTEGER NOT NULL,
            label VARCHAR(255) NOT NULL DEFAULT '',
            snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_assistant_config_version_profile_number UNIQUE (profile_id, number)
        )

        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_assistant_config_version_profile_id ON assistant_config_version (profile_id)")


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
