import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class BookingComplaintStatus(str, enum.Enum):
    open = "open"
    musician_accepted = "musician_accepted"
    musician_responded = "musician_responded"
    settled = "settled"


# Refund transfer lifecycle (string column — avoids PG enum migrations).
REFUND_STATUS_NONE = "none"
REFUND_STATUS_PROCESSING = "processing"
REFUND_STATUS_NEEDS_ATTENTION = "needs_attention"
REFUND_STATUS_AWAITING_TRANSFER = "awaiting_transfer"
REFUND_STATUS_AWAITING_VALIDATION = "awaiting_validation"
REFUND_STATUS_COMPLETED = "completed"
REFUND_STATUS_REJECTED = "rejected"


class BookingComplaint(Base):
    __tablename__ = "booking_complaint"
    __table_args__ = (
        CheckConstraint("(admin_musician_amount IS NULL OR admin_musician_amount >= 0) AND (admin_contractor_refund IS NULL OR admin_contractor_refund >= 0)", name="ck_booking_complaint_amounts_nonneg"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    opened_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=False
    )

    reason = Column(Text, nullable=False)
    evidence_url = Column(String, nullable=True)

    status = Column(
        Enum(BookingComplaintStatus, name="bookingcomplaintstatus"),
        nullable=False,
        default=BookingComplaintStatus.open,
    )

    musician_response = Column(Text, nullable=True)
    musician_response_evidence_url = Column(String, nullable=True)
    musician_responded_at = Column(UTCDateTime, nullable=True)

    # Set only by admin after both sides have responded (or musician accepted).
    admin_musician_amount = Column(Numeric(12, 2), nullable=True)
    admin_contractor_refund = Column(Numeric(12, 2), nullable=True)
    admin_notes = Column(Text, nullable=True)
    settled_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True)
    settled_at = Column(UTCDateTime, nullable=True)

    # Refund rail: admin transfers money back → contractor validates.
    refund_status = Column(String, nullable=False, default=REFUND_STATUS_NONE)
    refund_evidence_url = Column(String, nullable=True)
    refund_sent_at = Column(UTCDateTime, nullable=True)
    refund_sent_by_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=True
    )
    refund_validated_at = Column(UTCDateTime, nullable=True)
    refund_rejection_reason = Column(Text, nullable=True)
    refund_payment_id = Column(
        UUID(as_uuid=True), ForeignKey("payment.id"), nullable=True
    )

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking", back_populates="complaint")
    opened_by = relationship("User", foreign_keys=[opened_by_user_id])
    settled_by = relationship("User", foreign_keys=[settled_by_admin_id])
    refund_sent_by = relationship("User", foreign_keys=[refund_sent_by_admin_id])
