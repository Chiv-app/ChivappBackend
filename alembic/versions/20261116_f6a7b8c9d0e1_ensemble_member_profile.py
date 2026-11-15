"""Integrantes por perfil: cada integrante pertenece a un perfil artístico (un grupo), no solo a su líder

Agrega `ensemble_member.profile_id`, rellena los integrantes actuales con el perfil principal de su líder y
cambia la unicidad: antes un correo no podía repetirse por líder; ahora no puede repetirse por perfil
(una persona puede integrar dos grupos del mismo líder).

Es aditiva: no borra integrantes ni cambia convocatorias ni pagos.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKFILL_MEMBER_PROFILE_SQL = """
        UPDATE ensemble_member AS em
        SET profile_id = mp.id
        FROM musician_profile AS mp
        WHERE em.profile_id IS NULL
          AND mp.user_id = em.leader_user_id
          AND mp.is_primary
        """


def upgrade() -> None:
    # IF NOT EXISTS: el arranque de la app también crea esto (app/db/migrate.py).
    op.execute(
        "ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS profile_id UUID "
        "REFERENCES musician_profile(id) ON DELETE CASCADE"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ensemble_member_profile_id ON ensemble_member (profile_id)")
    op.execute(BACKFILL_MEMBER_PROFILE_SQL)
    op.execute("ALTER TABLE ensemble_member DROP CONSTRAINT IF EXISTS uq_ensemble_member_leader_email")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ensemble_member_profile_email "
        "ON ensemble_member (profile_id, email) WHERE profile_id IS NOT NULL"
    )


def downgrade() -> None:
    # Volver a un equipo por líder exigiría decidir a qué grupo pertenece cada integrante: se restaura
    # desde copia de seguridad.
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
