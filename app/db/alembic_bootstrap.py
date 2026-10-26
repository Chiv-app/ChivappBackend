"""Bootstrap Alembic version table for legacy databases created via Base.metadata.create_all.

If the database already contains core tables (e.g. 'user') but has no row in
'alembic_version', stamps the baseline revision ('c7a3bc7f5426') and ensures
tables added between the initial schema and 'c7a3bc7f5426' exist so that
'alembic upgrade head' applies incremental idempotent migrations ('d8e9f0a1b2c3')
without failing on pre-existing or missing intermediate tables.
"""

from __future__ import annotations

import logging
import os
import re

from sqlalchemy import Connection, create_engine, inspect as sa_inspect, pool, text

from app.core.config import settings
from app.db.session import Base
import app.models  # noqa: F401 - Register all models with Base.metadata

logger = logging.getLogger(__name__)

BASELINE_REVISION = "c7a3bc7f5426"
ALEMBIC_ADVISORY_LOCK_ID = 72839401
LEGACY_INTERMEDIATE_TABLES = ("booking_link", "payout_request", "uploaded_file")
_SAFE_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_schema_name(schema: str | None) -> str | None:
    if not schema:
        return None
    cleaned = schema.strip()
    if not _SAFE_SCHEMA_RE.fullmatch(cleaned):
        raise ValueError(f"Nombre de esquema PostgreSQL inválido: {schema!r}")
    return cleaned


def stamp_legacy_connection(conn: Connection, schema: str | None = None) -> bool:
    """Serialize migration bootstrap with an advisory lock and stamp legacy DBs."""
    target_schema = _validate_schema_name(schema or os.environ.get("ALEMBIC_SCHEMA"))

    if conn.dialect.name == "postgresql":
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": ALEMBIC_ADVISORY_LOCK_ID},
        )

    if target_schema:
        conn.execute(text(f'SET search_path TO "{target_schema}"'))

    inspector = sa_inspect(conn)
    existing_tables = set(inspector.get_table_names(schema=target_schema))
    if "user" not in existing_tables:
        return False

    # BUG6-01: Si la base existía desde antes de booking_link/payout_request/uploaded_file,
    # crear solo las tablas intermedias faltantes antes de aplicar d8e9f0a1b2c3.
    missing_table_objs = [
        Base.metadata.tables[t_name]
        for t_name in LEGACY_INTERMEDIATE_TABLES
        if t_name not in existing_tables and t_name in Base.metadata.tables
    ]
    if missing_table_objs:
        Base.metadata.create_all(bind=conn, tables=missing_table_objs, checkfirst=True)

    if "alembic_version" in existing_tables:
        row = conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).fetchone()
        if row is not None:
            return False

    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    conn.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
        {"rev": BASELINE_REVISION},
    )
    logger.info("Stamped legacy database with Alembic baseline %s", BASELINE_REVISION)
    return True


def ensure_alembic_stamped(database_uri: str | None = None, schema: str | None = None) -> bool:
    uri = database_uri or settings.SQLALCHEMY_DATABASE_URI
    engine = create_engine(uri, poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            return stamp_legacy_connection(conn, schema=schema)
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_alembic_stamped()
