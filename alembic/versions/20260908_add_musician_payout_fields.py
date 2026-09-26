"""add musician payout and payment settlement disbursement columns

Revision ID: 52c3d4e5f6a7
Revises: 41b2c3d4e5f6
Create Date: 2026-09-08 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "52c3d4e5f6a7"
down_revision: Union[str, None] = "41b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columns for MusicianProfile
    op.add_column("musician_profile", sa.Column("payout_method", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_bank_name", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_account_number", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_cci", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_phone", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_beneficiary_name", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_beneficiary_document", sa.String(), nullable=True))
    op.add_column("musician_profile", sa.Column("payout_mp_email", sa.String(), nullable=True))

    # Columns for Payment settlement disbursement
    op.add_column("payment", sa.Column("payout_reference", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("payout_evidence_url", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("payout_notes", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment", "payout_notes")
    op.drop_column("payment", "payout_evidence_url")
    op.drop_column("payment", "payout_reference")

    op.drop_column("musician_profile", "payout_mp_email")
    op.drop_column("musician_profile", "payout_beneficiary_document")
    op.drop_column("musician_profile", "payout_beneficiary_name")
    op.drop_column("musician_profile", "payout_phone")
    op.drop_column("musician_profile", "payout_cci")
    op.drop_column("musician_profile", "payout_account_number")
    op.drop_column("musician_profile", "payout_bank_name")
    op.drop_column("musician_profile", "payout_method")
