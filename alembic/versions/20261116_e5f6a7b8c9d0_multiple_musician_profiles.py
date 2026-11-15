"""Varios perfiles artísticos por persona

`musician_profile.user_id` deja de ser único. Cada persona conserva un perfil principal
(`is_primary`, único por persona); los demás son perfiles adicionales (hasta el límite que fija la app).

Es aditiva: todos los perfiles existentes quedan como principales y ningún dato cambia.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-16 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Quita cualquier restricción o índice único sobre user_id sola, sea cual sea su nombre en cada base.
DROP_USER_UNIQUE_SQL = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid = 'musician_profile'::regclass AND c.contype = 'u'
          AND array_length(c.conkey, 1) = 1
          AND c.conkey[1] = (SELECT attnum FROM pg_attribute WHERE attrelid = 'musician_profile'::regclass AND attname = 'user_id')
    LOOP
        EXECUTE format('ALTER TABLE musician_profile DROP CONSTRAINT %I', r.conname);
    END LOOP;
    FOR r IN
        SELECT i.indexrelid::regclass::text AS idx
        FROM pg_index i
        WHERE i.indrelid = 'musician_profile'::regclass AND i.indisunique AND NOT i.indisprimary
          AND i.indpred IS NULL AND i.indnatts = 1
          AND i.indkey[0] = (SELECT attnum FROM pg_attribute WHERE attrelid = 'musician_profile'::regclass AND attname = 'user_id')
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
    LOOP
        EXECUTE format('DROP INDEX %s', r.idx);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    # IF NOT EXISTS: el arranque de la app también crea esto (app/db/migrate.py).
    op.execute("ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute(DROP_USER_UNIQUE_SQL)
    op.execute("CREATE INDEX IF NOT EXISTS ix_musician_profile_user_id ON musician_profile (user_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_one_primary ON musician_profile (user_id) WHERE is_primary")


def downgrade() -> None:
    # Volver a un perfil por persona exige decidir qué perfiles conservar: se restaura desde copia de seguridad.
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
