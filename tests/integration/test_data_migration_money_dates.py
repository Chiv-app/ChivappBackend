"""La migración b2c3d4e5f6a8 convierte datos existentes sin perder información.

Se arma un esquema temporal en la revisión anterior (columnas con el tipo viejo), se insertan
filas y se sube a `head` comprobando el resultado.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.core.config import settings

pytestmark = pytest.mark.db

BACKEND_DIR = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "a1b2c3d4e5f7"


def _alembic(db_url: str, schema: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ENVIRONMENT"] = "development"
    env["SQLALCHEMY_DATABASE_URI"] = db_url
    env["ALEMBIC_SCHEMA"] = schema
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_money_and_timestamps_are_converted_keeping_the_data():
    db_url = settings.SQLALCHEMY_DATABASE_URI
    engine = sa.create_engine(db_url, poolclass=sa.pool.NullPool)
    schema = f"test_mig_data_{uuid.uuid4().hex[:8]}"
    audit_id = uuid.uuid4()
    settings_id = uuid.uuid4()

    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

        res = _alembic(db_url, schema, "upgrade", PREVIOUS_REVISION)
        assert res.returncode == 0, res.stderr

        with engine.begin() as conn:
            conn.execute(sa.text(f'SET search_path TO "{schema}"'))
            kinds = dict(
                conn.execute(
                    sa.text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = 'admin_audit_log'"
                    ),
                    {"s": schema},
                ).fetchall()
            )
            assert kinds["created_at"] == "timestamp without time zone"
            # Datos "antiguos": hora UTC sin zona y un porcentaje con más de 2 decimales.
            conn.execute(
                sa.text(
                    "INSERT INTO admin_audit_log (id, action, target_type, created_at) "
                    "VALUES (:id, 'test', 'booking', '2026-11-05 15:30:00')"
                ),
                {"id": audit_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO platform_payment_settings (id, phone_number, phone_label, platform_fee_percent, created_at) "
                    "VALUES (:id, '', 'Yape', 2.456, '2026-11-05 15:30:00')"
                ),
                {"id": settings_id},
            )

        res_up = _alembic(db_url, schema, "upgrade", "head")
        assert res_up.returncode == 0, res_up.stderr

        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{schema}"'))
            info = {
                (r[0], r[1]): (r[2], r[3], r[4])
                for r in conn.execute(
                    sa.text(
                        "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
                        "FROM information_schema.columns WHERE table_schema = :s"
                    ),
                    {"s": schema},
                )
            }
            assert info[("admin_audit_log", "created_at")][0] == "timestamp with time zone"
            assert info[("payment", "amount")][:3] == ("numeric", 12, 2)
            assert info[("booking", "price_agreed")][:3] == ("numeric", 12, 2)
            assert info[("platform_payment_settings", "platform_fee_percent")][:3] == ("numeric", 5, 2)

            # La hora se conserva como el mismo instante UTC.
            created = conn.execute(
                sa.text("SELECT created_at AT TIME ZONE 'UTC' FROM admin_audit_log WHERE id = :id"),
                {"id": audit_id},
            ).scalar()
            assert created.isoformat() == "2026-11-05T15:30:00"

            # El porcentaje se redondea a 2 decimales.
            percent = conn.execute(
                sa.text("SELECT platform_fee_percent FROM platform_payment_settings WHERE id = :id"),
                {"id": settings_id},
            ).scalar()
            assert str(percent) == "2.46"

            # Las restricciones existen y, sin filas que las violen, quedan validadas.
            checks = dict(
                conn.execute(
                    sa.text(
                        "SELECT conname, convalidated FROM pg_constraint "
                        "WHERE conname LIKE 'ck\\_%' AND connamespace = current_schema()::regnamespace"
                    )
                ).fetchall()
            )
            assert checks["ck_payment_amount_nonneg"] is True
            assert checks["ck_booking_review_rating_range"] is True
            assert checks["ck_musician_availability_day_range"] is True

        # Es idempotente: volver a ejecutar no falla ni cambia nada.
        again = _alembic(db_url, schema, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        check = _alembic(db_url, schema, "check")
        assert check.returncode == 0, check.stderr
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
