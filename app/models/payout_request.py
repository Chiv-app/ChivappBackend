
import uuid
from datetime import datetime
from sqlalchemy import CheckConstraint, Column, String, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base
from app.db.types import UTCDateTime


class PayoutRequest(Base):
    __tablename__ = 'payout_request'
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payout_request_amount_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    musician_id = Column(UUID(as_uuid=True), ForeignKey('musician_profile.id'), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String, default='pending', nullable=False)  # pending, processed, rejected
    notes = Column(String, nullable=True)
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    processed_at = Column(UTCDateTime, nullable=True)


class PayoutRequestBooking(Base):
    """Asociación explícita entre una solicitud de desembolso y las reservas que cubre."""

    __tablename__ = 'payout_request_booking'

    payout_request_id = Column(
        UUID(as_uuid=True),
        ForeignKey('payout_request.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey('booking.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
        index=True,
    )
    created_at = Column(UTCDateTime, default=datetime.utcnow)

