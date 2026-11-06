import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.session import Base


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    payment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("payment.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    entry_type = Column(String, nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String, nullable=False, default="PEN", server_default="PEN")
    description = Column(String, nullable=True)
    meta = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
