"""Tipo de perfil (solista o grupo), tamaño del grupo y datos de detalle del perfil artístico

Agrega a `musician_profile`:
- `profile_type`: «solo» o «group». Los perfiles que ya tienen integrantes (invitados o activos)
  pasan a «group»; el resto queda como «solo».
- `group_size`: cuántas personas son, solo para grupos. Queda vacío en los perfiles existentes.
- `details`: datos opcionales validados (eventos, show, reservas, experiencia…), vacío al inicio.

Es aditiva: no borra ni cambia ninguna columna existente.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b9
Create Date: 2026-11-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKFILL_GROUPS_SQL = """
        UPDATE musician_profile AS mp
        SET profile_type = 'group'
        WHERE mp.profile_type = 'solo'
          AND EXISTS (
              SELECT 1 FROM ensemble_member AS em
              WHERE em.leader_user_id = mp.user_id
                AND em.status::text IN ('invited', 'active')
          )
        """


def _add_check(name: str, expression: str) -> None:
    # Idempotente: el arranque de la app y las pruebas también pueden haber creado la tabla.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{name}'
            ) THEN
                ALTER TABLE musician_profile ADD CONSTRAINT {name} CHECK ({expression});
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    # IF NOT EXISTS: el arranque de la app también crea estas columnas (app/db/migrate.py).
    op.execute("ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS profile_type VARCHAR(16) NOT NULL DEFAULT 'solo'")
    op.execute("ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS group_size VARCHAR(8)")
    op.execute("ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute(BACKFILL_GROUPS_SQL)
    _add_check("ck_musician_profile_type", "profile_type IN ('solo', 'group')")
    _add_check(
        "ck_musician_profile_group_size",
        "group_size IS NULL OR group_size IN ('2', '3', '4-5', '6-8', '9+')",
    )


def downgrade() -> None:
    # Las columnas se pueden borrar, pero se perdería lo que los músicos completaron: se exige restaurar
    # desde copia de seguridad.
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
