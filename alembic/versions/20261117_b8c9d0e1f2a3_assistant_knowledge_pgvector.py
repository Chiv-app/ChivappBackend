"""Búsqueda semántica del asistente IA: extensión pgvector y tabla de fragmentos de conocimiento

Activa la extensión `vector` (si el servidor la ofrece) y crea `assistant_knowledge_chunk`, donde se guardan los
fragmentos de la contrata, la política de cancelación, el perfil y los paquetes de cada músico con su embedding.

Es tolerante: si el servidor no tiene pgvector o el usuario no puede crear la extensión, la migración no falla y
el asistente sigue funcionando sin búsqueda semántica (envía todo el conocimiento como contexto).
La tabla no está en el ORM a propósito (su columna `vector` necesita la extensión); `alembic/env.py` la excluye
de la comparación del modelo.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-17 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
                BEGIN
                    CREATE EXTENSION IF NOT EXISTS vector;
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'No se pudo crear la extension vector';
                END;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
    DECLARE
        ext_schema text;
    BEGIN
        SELECT n.nspname INTO ext_schema
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector';
        IF ext_schema IS NOT NULL THEN
            -- El tipo `vector` vive en el esquema donde se instaló la extensión (no siempre el de la sesión).
            EXECUTE $q$
                CREATE TABLE IF NOT EXISTS assistant_knowledge_chunk (
                    id UUID PRIMARY KEY,
                    profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
                    source VARCHAR(32) NOT NULL,
                    source_ref VARCHAR(120) NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    content_hash VARCHAR(64) NOT NULL,
                    embedding $q$ || quote_ident(ext_schema) || $q$.vector(768) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            $q$;
            CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_chunk_profile
                ON assistant_knowledge_chunk (profile_id);
            EXECUTE $q$
                CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_chunk_embedding
                ON assistant_knowledge_chunk USING hnsw (embedding $q$ || quote_ident(ext_schema) || $q$.vector_cosine_ops)
            $q$;
        END IF;
    END
    $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
