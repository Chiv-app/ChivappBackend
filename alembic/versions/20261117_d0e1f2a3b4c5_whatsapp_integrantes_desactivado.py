"""WhatsApp del asistente: el filtro de integrantes viene desactivado por defecto

Cambia el valor por defecto de `musician_whatsapp_channel.ignore_ensemble_members` a FALSE y apaga el filtro en los
canales ya creados, que lo tenían encendido solo porque era el valor por defecto (aún no hay músicos usándolo).

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-11-17 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE musician_whatsapp_channel ALTER COLUMN ignore_ensemble_members SET DEFAULT FALSE")
    op.execute("UPDATE musician_whatsapp_channel SET ignore_ensemble_members = FALSE WHERE ignore_ensemble_members IS TRUE")


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
