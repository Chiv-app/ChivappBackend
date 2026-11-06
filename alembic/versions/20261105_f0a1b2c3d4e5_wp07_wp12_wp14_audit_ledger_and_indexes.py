"""wp07_wp12_wp14_audit_ledger_and_indexes

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-11-05 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. WP-07: Tabla de auditoría de acciones administrativas (`admin_audit_log`)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id UUID PRIMARY KEY,
            admin_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
            action VARCHAR NOT NULL,
            target_type VARCHAR NOT NULL,
            target_id VARCHAR,
            before_data JSONB,
            after_data JSONB,
            ip_address VARCHAR,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_audit_log_admin_user_id ON admin_audit_log (admin_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_audit_log_action ON admin_audit_log (action)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_audit_log_target_id ON admin_audit_log (target_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_audit_log_created_at ON admin_audit_log (created_at)")

    # 2. WP-14: Tabla inmutable de libro mayor (`ledger_entry`)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger_entry (
            id UUID PRIMARY KEY,
            booking_id UUID REFERENCES booking(id) ON DELETE SET NULL,
            payment_id UUID REFERENCES payment(id) ON DELETE SET NULL,
            actor_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
            entry_type VARCHAR NOT NULL,
            amount NUMERIC(12, 2) NOT NULL,
            currency VARCHAR NOT NULL DEFAULT 'PEN',
            description VARCHAR,
            meta JSONB,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entry_booking_id ON ledger_entry (booking_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entry_payment_id ON ledger_entry (payment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entry_actor_user_id ON ledger_entry (actor_user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entry_entry_type ON ledger_entry (entry_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ledger_entry_created_at ON ledger_entry (created_at)")

    # 3. WP-12 (D-01): Índices compuestos y de claves foráneas de alto tráfico
    op.execute("CREATE INDEX IF NOT EXISTS ix_booking_contractor_created_at ON booking (contractor_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_booking_musician_event_date_status ON booking (musician_id, event_date, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_booking_status_updated_at ON booking (status, updated_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notification_user_read_created_at ON notification (user_id, is_read, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_payment_booking_id ON payment (booking_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_musician_availability_musician_id ON musician_availability (musician_id)")


def downgrade() -> None:
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
