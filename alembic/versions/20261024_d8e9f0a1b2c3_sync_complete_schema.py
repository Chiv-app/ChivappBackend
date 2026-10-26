"""sync_complete_schema

Revision ID: d8e9f0a1b2c3
Revises: c7a3bc7f5426
Create Date: 2026-10-24 00:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'c7a3bc7f5426'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_unique_index_checked(
    index_name: str,
    table_sql: str,
    key_expr: str,
    where_clause: str,
    create_index_sql: str,
) -> None:
    """Detect duplicate keys before creating a unique index and fail with a clear message."""
    bind = op.get_bind()
    already_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :idx"
        ),
        {"idx": index_name},
    ).scalar()
    if already_exists:
        return

    where_sql = f"WHERE {where_clause}" if where_clause else ""
    dup_query = f"""
        SELECT {key_expr} AS dup_key, COUNT(*) AS dup_count
        FROM {table_sql}
        {where_sql}
        GROUP BY {key_expr}
        HAVING COUNT(*) > 1
    """
    dups = bind.execute(sa.text(dup_query)).fetchall()
    if dups:
        sample = ", ".join(f"{row[0]!r} (x{row[1]})" for row in dups[:5])
        remediation_sql = (
            f"SELECT {key_expr}, COUNT(*) FROM {table_sql} "
            f"{where_sql} GROUP BY 1 HAVING COUNT(*) > 1;"
        )
        raise RuntimeError(
            f"Migración abortada: no se puede crear el índice único '{index_name}' en {table_sql} "
            f"porque existen {len(dups)} valor(es) duplicado(s): {sample}. "
            f"Para inspeccionar y resolver los duplicados antes de migrar ejecuta: {remediation_sql}"
        )

    op.execute(create_index_sql)


def upgrade() -> None:
    """Ensure all columns, tables, and partial indexes from models exist."""
    # 1. Missing user columns & nullability
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS google_calendar_refresh_token VARCHAR')
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0')
    op.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS login_otp_attempts INTEGER NOT NULL DEFAULT 0')
    op.execute('ALTER TABLE "user" ALTER COLUMN fullname DROP NOT NULL')

    # 2. Missing musician_profile columns & nullability
    op.execute('ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS is_ensemble_only BOOLEAN NOT NULL DEFAULT FALSE')
    op.execute('ALTER TABLE musician_profile ALTER COLUMN stage_name DROP NOT NULL')

    # 3. Missing booking cancellation/refund columns
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP')
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_percent NUMERIC')
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_amount NUMERIC')
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_status VARCHAR')
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_error VARCHAR')
    op.execute('ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refunded_at TIMESTAMP')

    # 4. Missing payment gateway columns
    op.execute('ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_provider VARCHAR')
    op.execute('ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_payment_id VARCHAR')
    op.execute('ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_preference_id VARCHAR')
    op.execute('ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_metadata JSONB')
    op.execute('CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_id ON payment (gateway_payment_id)')

    # 5. Missing tables for legacy schemas created before Oct 9-17 (BUG6-01)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'bookinglinkstatus') THEN
                CREATE TYPE bookinglinkstatus AS ENUM ('active', 'completed', 'expired');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_link (
            id UUID PRIMARY KEY,
            token VARCHAR NOT NULL,
            musician_id UUID NOT NULL REFERENCES musician_profile(id),
            price NUMERIC NOT NULL,
            event_date DATE,
            start_time TIME WITHOUT TIME ZONE,
            client_email VARCHAR,
            status bookinglinkstatus NOT NULL,
            expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            booking_id UUID REFERENCES booking(id),
            created_at TIMESTAMP WITHOUT TIME ZONE,
            updated_at TIMESTAMP WITHOUT TIME ZONE
        )
        """
    )
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_link_token ON booking_link (token)')

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS payout_request (
            id UUID PRIMARY KEY,
            musician_id UUID NOT NULL REFERENCES musician_profile(id),
            amount NUMERIC NOT NULL,
            status VARCHAR NOT NULL,
            notes VARCHAR,
            created_at TIMESTAMP WITHOUT TIME ZONE,
            processed_at TIMESTAMP WITHOUT TIME ZONE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS uploaded_file (
            id UUID PRIMARY KEY,
            filename VARCHAR NOT NULL,
            owner_user_id UUID NOT NULL REFERENCES "user"(id),
            content_type VARCHAR NOT NULL,
            size_bytes INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute('ALTER TABLE uploaded_file DROP CONSTRAINT IF EXISTS uploaded_file_filename_key')
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname = 'ix_uploaded_file_filename'
                  AND NOT i.indisunique
            ) THEN
                DROP INDEX ix_uploaded_file_filename;
            END IF;
        END $$;
        """
    )
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_uploaded_file_filename ON uploaded_file (filename)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_uploaded_file_owner_user_id ON uploaded_file (owner_user_id)')

    _create_unique_index_checked(
        index_name="ix_musician_profile_slug",
        table_sql="musician_profile",
        key_expr="slug",
        where_clause="slug IS NOT NULL",
        create_index_sql="CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_profile_slug ON musician_profile (slug)",
    )
    _create_unique_index_checked(
        index_name="ix_user_username",
        table_sql='"user"',
        key_expr="username",
        where_clause="username IS NOT NULL",
        create_index_sql='CREATE UNIQUE INDEX IF NOT EXISTS ix_user_username ON "user" (username)',
    )

    # 6. Partial unique indexes for business invariants (BUG6-02: checked for duplicates first)
    _create_unique_index_checked(
        index_name="uq_payment_gateway_payment_id",
        table_sql="payment",
        key_expr="gateway_payment_id",
        where_clause="gateway_payment_id IS NOT NULL AND payment_type IS DISTINCT FROM 'refund'",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_gateway_payment_id ON payment (gateway_payment_id) "
            "WHERE gateway_payment_id IS NOT NULL AND payment_type IS DISTINCT FROM 'refund'"
        ),
    )
    _create_unique_index_checked(
        index_name="uq_booking_review_final",
        table_sql="booking_review",
        key_expr="booking_id",
        where_clause="is_final IS TRUE",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_review_final "
            "ON booking_review (booking_id) WHERE is_final IS TRUE"
        ),
    )
    _create_unique_index_checked(
        index_name="uq_musician_profile_stage_name_lower",
        table_sql="musician_profile",
        key_expr="lower(btrim(stage_name))",
        where_clause="stage_name IS NOT NULL AND btrim(stage_name) <> ''",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_stage_name_lower "
            "ON musician_profile (lower(btrim(stage_name))) "
            "WHERE stage_name IS NOT NULL AND btrim(stage_name) <> ''"
        ),
    )
    _create_unique_index_checked(
        index_name="uq_user_phone_normalized",
        table_sql='"user"',
        key_expr="phone",
        where_clause="phone IS NOT NULL AND btrim(phone) <> ''",
        create_index_sql=(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_phone_normalized '
            'ON "user" (phone) WHERE phone IS NOT NULL AND btrim(phone) <> \'\''
        ),
    )
    _create_unique_index_checked(
        index_name="uq_user_username_lower",
        table_sql='"user"',
        key_expr="lower(btrim(username))",
        where_clause="username IS NOT NULL AND btrim(username) <> ''",
        create_index_sql=(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username_lower '
            'ON "user" (lower(btrim(username))) WHERE username IS NOT NULL AND btrim(username) <> \'\''
        ),
    )
    _create_unique_index_checked(
        index_name="uq_musician_profile_slug_lower",
        table_sql="musician_profile",
        key_expr="lower(btrim(slug))",
        where_clause="slug IS NOT NULL AND btrim(slug) <> ''",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_slug_lower "
            "ON musician_profile (lower(btrim(slug))) WHERE slug IS NOT NULL AND btrim(slug) <> ''"
        ),
    )
    _create_unique_index_checked(
        index_name="uq_contractor_document_number_normalized",
        table_sql="contractor_profile",
        key_expr="upper(replace(btrim(document_number), ' ', ''))",
        where_clause="document_number IS NOT NULL AND btrim(document_number) <> ''",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_contractor_document_number_normalized "
            "ON contractor_profile (upper(replace(btrim(document_number), ' ', ''))) "
            "WHERE document_number IS NOT NULL AND btrim(document_number) <> ''"
        ),
    )
    _create_unique_index_checked(
        index_name="uq_user_email_verification_token",
        table_sql='"user"',
        key_expr="email_verification_token",
        where_clause="email_verification_token IS NOT NULL",
        create_index_sql=(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email_verification_token '
            'ON "user" (email_verification_token) WHERE email_verification_token IS NOT NULL'
        ),
    )
    _create_unique_index_checked(
        index_name="uq_user_password_reset_token",
        table_sql='"user"',
        key_expr="password_reset_token",
        where_clause="password_reset_token IS NOT NULL",
        create_index_sql=(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_password_reset_token '
            'ON "user" (password_reset_token) WHERE password_reset_token IS NOT NULL'
        ),
    )
    _create_unique_index_checked(
        index_name="uq_payout_request_musician_pending",
        table_sql="payout_request",
        key_expr="musician_id",
        where_clause="status = 'pending'",
        create_index_sql=(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payout_request_musician_pending "
            "ON payout_request (musician_id) WHERE status = 'pending'"
        ),
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
