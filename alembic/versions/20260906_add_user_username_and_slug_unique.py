"""add user username and musician slug unique

Revision ID: 41b2c3d4e5f6
Revises: 26f1c3cac4bc
Create Date: 2026-09-06 21:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "41b2c3d4e5f6"
down_revision: Union[str, None] = "26f1c3cac4bc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user", sa.Column("username", sa.String(), nullable=True))
    op.create_index(
        "ix_user_username",
        "user",
        ["username"],
        unique=True,
    )
    op.create_index(
        "ix_musician_profile_slug",
        "musician_profile",
        ["slug"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError('Downgrade no soportado; restaura desde copia de seguridad.')
