"""Bootstrap Alembic version table for legacy databases created via Base.metadata.create_all.

If the database already contains core tables (e.g. 'user') but has no row in
'alembic_version', stamps the baseline revision ('c7a3bc7f5426') so that
'alembic upgrade head' applies incremental idempotent migrations ('d8e9f0a1b2c3')
without failing on pre-existing tables.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, inspect as sa_inspect, pool, text

from app.core.config import settings

logger = logging.getLogger(__name__)

BASELINE_REVISION = "c7a3bc7f5426"


def ensure_alembic_stamped(database_uri: str | None = None, schema: str | None = None) -> bool:
    uri = database_uri or settings.SQLALCHEMY_DATABASE_URI
    target_schema = schema or os.environ.get("ALEMBIC_SCHEMA") or None

    engine = create_engine(uri, poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            if target_schema:
                conn.execute(text(f"SET search_path TO {target_schema}"))

            inspector = sa_inspect(conn)
            existing_tables = set(inspector.get_table_names(schema=target_schema))
            if "user" not in existing_tables:
                return False

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
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_alembic_stamped()
