"""Seguimiento de aperturas, embudo por pasos y auto-guardado en links de pago

Agrega a `booking_link`:
- `open_count`: contador de aperturas reales del cliente.
- `first_opened_at`, `last_opened_at`, `last_activity_at`: marcas de tiempo con zona horaria.
- `current_step`, `max_step_reached`: paso actual y máximo alcanzado por el cliente (0 a 4).
- `draft_data`: objeto JSONB con el borrador auto-guardado o guardado manualmente por el cliente.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-11-24 00:50:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS open_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS first_opened_at TIMESTAMPTZ")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS last_opened_at TIMESTAMPTZ")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS current_step INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS max_step_reached INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS draft_data JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
