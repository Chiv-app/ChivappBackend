"""Add calendar_event_id to Booking

Revision ID: 3b1c2d3e4f5g
Revises: 52c3d4e5f6a7
Create Date: 2026-09-21 23:37:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3b1c2d3e4f5g'
down_revision = '52c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('booking', sa.Column('calendar_event_id', sa.String(), nullable=True))


def downgrade():
    op.drop_column('booking', 'calendar_event_id')
