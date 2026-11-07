"""money_precision_timestamptz_and_checks

Tres arreglos de integridad de datos (ver docs/MIGRACION_DATOS_PRODUCCION.md):

1. Dinero con decimales exactos: montos en NUMERIC(12,2) y porcentajes en NUMERIC(5,2).
2. Fechas con zona horaria: todas las columnas `timestamp without time zone` pasan a
   `timestamp with time zone`. Los valores existentes se guardaron en UTC (la aplicación usa
   `datetime.utcnow()`), por eso se convierten con `AT TIME ZONE 'UTC'`.
3. Restricciones CHECK (montos no negativos, calificación 1-5, día de la semana 0-6). Se crean
   primero sin validar (no bloquean escrituras) y luego se intenta validarlas: si existen filas
   anteriores que no las cumplen, la restricción queda activa para filas nuevas y sin validar
   para las antiguas; se corrige y se ejecuta `VALIDATE CONSTRAINT` (ver la guía).

Es idempotente: se puede volver a ejecutar sobre una base que ya tenga los cambios.

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-11-06 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (tabla, columna, tipo nuevo)
MONEY_COLUMNS: list[tuple[str, str, str]] = [
    ("booking", "price_agreed", "NUMERIC(12,2)"),
    ("booking", "platform_fee_amount", "NUMERIC(12,2)"),
    ("booking", "cancellation_refund_amount", "NUMERIC(12,2)"),
    ("booking", "pending_price_agreed", "NUMERIC(12,2)"),
    ("booking", "platform_fee_percent", "NUMERIC(5,2)"),
    ("booking", "cancellation_refund_percent", "NUMERIC(5,2)"),
    ("booking_complaint", "admin_musician_amount", "NUMERIC(12,2)"),
    ("booking_complaint", "admin_contractor_refund", "NUMERIC(12,2)"),
    ("booking_link", "price", "NUMERIC(12,2)"),
    ("payment", "amount", "NUMERIC(12,2)"),
    ("payout_request", "amount", "NUMERIC(12,2)"),
    ("platform_payment_settings", "platform_fee_percent", "NUMERIC(5,2)"),
]

# (tabla, nombre, expresión)
CHECKS: list[tuple[str, str, str]] = [
    ("booking", "ck_booking_price_agreed_positive", "price_agreed IS NULL OR price_agreed > 0"),
    ("booking", "ck_booking_fee_amount_nonneg", "platform_fee_amount IS NULL OR platform_fee_amount >= 0"),
    (
        "booking",
        "ck_booking_refund_amount_nonneg",
        "cancellation_refund_amount IS NULL OR cancellation_refund_amount >= 0",
    ),
    ("booking_review", "ck_booking_review_rating_range", "rating BETWEEN 1 AND 5"),
    (
        "booking_complaint",
        "ck_booking_complaint_amounts_nonneg",
        "(admin_musician_amount IS NULL OR admin_musician_amount >= 0) "
        "AND (admin_contractor_refund IS NULL OR admin_contractor_refund >= 0)",
    ),
    ("booking_link", "ck_booking_link_price_positive", "price > 0"),
    ("musician_availability", "ck_musician_availability_day_range", "day_of_week BETWEEN 0 AND 6"),
    ("payout_request", "ck_payout_request_amount_positive", "amount > 0"),
    ("payment", "ck_payment_amount_nonneg", "amount >= 0"),
]


def _existing_columns(bind) -> dict[tuple[str, str], str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
            """
        )
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _existing_columns(bind)

    # 1. Dinero con decimales exactos (una sola reescritura por tabla)
    by_table: dict[str, list[str]] = {}
    for table, column, new_type in MONEY_COLUMNS:
        if (table, column) not in columns:
            continue
        clause = (
            f'ALTER COLUMN "{column}" TYPE {new_type} '
            f'USING ROUND("{column}"::numeric, 2)'
        )
        by_table.setdefault(table, []).append(clause)
    for table, clauses in by_table.items():
        op.execute(f'ALTER TABLE "{table}" ' + ", ".join(clauses))

    # 2. Fechas con zona horaria: los valores guardados son UTC
    timestamp_by_table: dict[str, list[str]] = {}
    for (table, column), data_type in sorted(columns.items()):
        if data_type != "timestamp without time zone" or table == "alembic_version":
            continue
        timestamp_by_table.setdefault(table, []).append(
            f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE '
            f"USING \"{column}\" AT TIME ZONE 'UTC'"
        )
    for table, clauses in timestamp_by_table.items():
        op.execute(f'ALTER TABLE "{table}" ' + ", ".join(clauses))

    # 3. Restricciones CHECK: primero sin validar, luego se intenta validar
    for table, name, expr in CHECKS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('{table}') IS NOT NULL THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{name}' AND conrelid = to_regclass('{table}')
                    ) THEN
                        ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID;
                    END IF;
                    BEGIN
                        ALTER TABLE {table} VALIDATE CONSTRAINT {name};
                    EXCEPTION WHEN check_violation THEN
                        RAISE NOTICE 'La restriccion {name} queda SIN VALIDAR: hay filas anteriores que no la cumplen. Corrigelas y ejecuta VALIDATE CONSTRAINT.';
                    END;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
