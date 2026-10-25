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


def upgrade() -> None:
    """Ensure all columns, tables, and partial indexes from models/migrate.py exist."""
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

    # 5. Missing uploaded_file table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS uploaded_file (
            id UUID PRIMARY KEY,
            filename VARCHAR NOT NULL UNIQUE,
            owner_user_id UUID NOT NULL REFERENCES "user"(id),
            content_type VARCHAR NOT NULL,
            size_bytes INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute('CREATE INDEX IF NOT EXISTS ix_uploaded_file_filename ON uploaded_file (filename)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_uploaded_file_owner_user_id ON uploaded_file (owner_user_id)')

    # 6. Partial unique indexes for business invariants
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_gateway_payment_id ON payment (gateway_payment_id) "
        "WHERE gateway_payment_id IS NOT NULL AND payment_type IS DISTINCT FROM 'refund'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_review_final "
        "ON booking_review (booking_id) WHERE is_final IS TRUE"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_stage_name_lower "
        "ON musician_profile (lower(btrim(stage_name)))"
    )
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_phone_normalized '
        'ON "user" (phone) WHERE phone IS NOT NULL AND btrim(phone) <> \'\''
    )
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username_lower '
        'ON "user" (lower(btrim(username))) WHERE username IS NOT NULL AND btrim(username) <> \'\''
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_slug_lower "
        "ON musician_profile (lower(btrim(slug))) WHERE slug IS NOT NULL AND btrim(slug) <> ''"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_contractor_document_number_normalized "
        "ON contractor_profile (upper(replace(btrim(document_number), ' ', ''))) "
        "WHERE document_number IS NOT NULL AND btrim(document_number) <> ''"
    )
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email_verification_token '
        'ON "user" (email_verification_token) WHERE email_verification_token IS NOT NULL'
    )
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_password_reset_token '
        'ON "user" (password_reset_token) WHERE password_reset_token IS NOT NULL'
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_payout_request_musician_pending "
        "ON payout_request (musician_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP INDEX IF EXISTS uq_payment_gateway_payment_id')
