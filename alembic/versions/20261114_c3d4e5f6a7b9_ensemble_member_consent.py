"""Consentimiento de integrantes: quién terminó la relación y reconfirmación de activos sin aceptar

Agrega `ended_by` y `ended_at` a `ensemble_member` (para limitar las reinvitaciones tras un rechazo) y
pasa a «invited» a los integrantes que un líder activó antes de que existiera el consentimiento:
músicos con perfil propio, activos, que nunca respondieron ni aceptaron una convocatoria. Siguen viendo
las reservas en las que ya estaban; para recibir nuevas convocatorias deben aceptar la invitación, que
verán en su panel.

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-11-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RECONFIRM_UNCONSENTED_MEMBERS_SQL = """
        UPDATE ensemble_member AS em
        SET status = 'invited', joined_at = NULL
        FROM musician_profile AS mp
        WHERE mp.user_id = em.member_user_id
          AND em.status::text = 'active'
          AND mp.is_ensemble_only IS NOT TRUE
          AND NOT EXISTS (
              SELECT 1 FROM notification AS n
              WHERE n.user_id = em.leader_user_id
                AND n.type = 'ensemble_member_joined'
                AND n.meta ->> 'ensemble_member_id' = em.id::text
          )
          AND NOT EXISTS (
              SELECT 1 FROM booking_member_invite AS bmi
              WHERE bmi.ensemble_member_id = em.id
                AND bmi.status::text = 'accepted'
          )
        """


def upgrade() -> None:
    # IF NOT EXISTS: el arranque de la app también crea estas columnas (app/db/migrate.py).
    op.execute("ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS ended_by VARCHAR(16)")
    op.execute("ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ")
    op.execute(RECONFIRM_UNCONSENTED_MEMBERS_SQL)


def downgrade() -> None:
    # Las columnas se pueden borrar, pero el estado anterior de los integrantes no se restaura (no hay
    # forma de saber quién estaba activo): se exige restaurar desde copia de seguridad.
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
