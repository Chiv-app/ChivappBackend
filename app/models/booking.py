import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    String,
    Date,
    Time,
    Numeric,
    Enum,
    ForeignKey,
    Text,
    Integer,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime
import enum


class BookingStatus(str, enum.Enum):
    requested = "requested"
    accepted = "accepted"
    contract_pending = "contract_pending"
    contract_signed = "contract_signed"
    payment_retained = "payment_retained"  # Reserva confirmada (pago total retenido)
    change_pending = "change_pending"
    in_progress = "in_progress"
    payment_released = "payment_released"
    completed = "completed"
    cancelled = "cancelled"


class Booking(Base):
    __tablename__ = "booking"
    __table_args__ = (
        CheckConstraint("price_agreed IS NULL OR price_agreed > 0", name="ck_booking_price_agreed_positive"),
        CheckConstraint("platform_fee_amount IS NULL OR platform_fee_amount >= 0", name="ck_booking_fee_amount_nonneg"),
        CheckConstraint("cancellation_refund_amount IS NULL OR cancellation_refund_amount >= 0", name="ck_booking_refund_amount_nonneg"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    contractor_id = Column(UUID(as_uuid=True),
                           ForeignKey("contractor_profile.id"))
    musician_id = Column(UUID(as_uuid=True),
                         ForeignKey("musician_profile.id"))

    event_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=True)

    location_address = Column(String, nullable=False)
    location_city = Column(String, nullable=True)
    location_reference = Column(String, nullable=True)

    event_type = Column(String, nullable=False)
    event_description = Column(String, nullable=True)
    # Temas del repertorio del músico elegidos por el contratista.
    requested_repertoire = Column(JSONB, nullable=True)

    price_agreed = Column(Numeric(12, 2), nullable=True)
    # Snapshot: comisión de plataforma sobre price_agreed (la paga el contratista).
    platform_fee_percent = Column(Numeric(5, 2), nullable=True)
    platform_fee_amount = Column(Numeric(12, 2), nullable=True)

    musician_quote_notes = Column(String, nullable=True)
    quoted_at = Column(UTCDateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)
    cancelled_by = Column(String, nullable=True)
    cancelled_at = Column(UTCDateTime, nullable=True)
    # Reembolso aplicado por la política de cancelación.
    # cancellation_refund_status: none | processing | completed | failed
    cancellation_refund_percent = Column(Numeric(5, 2), nullable=True)
    cancellation_refund_amount = Column(Numeric(12, 2), nullable=True)
    cancellation_refund_status = Column(String, nullable=True)
    cancellation_refund_error = Column(String, nullable=True)
    cancellation_refunded_at = Column(UTCDateTime, nullable=True)

    # Pending event changes proposed by either party (awaiting accept/reject)
    pending_location_address = Column(String, nullable=True)
    pending_location_city = Column(String, nullable=True)
    pending_location_reference = Column(String, nullable=True)
    pending_event_description = Column(String, nullable=True)
    pending_change_notes = Column(String, nullable=True)
    pending_price_agreed = Column(Numeric(12, 2), nullable=True)
    change_requested_by = Column(String, nullable=True)  # "contractor" | "musician"
    change_requested_at = Column(UTCDateTime, nullable=True)

    # Integraciones externas
    calendar_event_id = Column(String, nullable=True)

    # Guest share link (enabled once the booking is paid/confirmed)
    share_token = Column(String, unique=True, nullable=True, index=True)
    share_enabled = Column(Boolean, nullable=False, default=False)
    share_enabled_at = Column(UTCDateTime, nullable=True)

    status = Column(Enum(BookingStatus),
                    default=BookingStatus.requested,
                    nullable=False)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    contractor = relationship("ContractorProfile")
    musician = relationship("MusicianProfile")
    messages = relationship(
        "BookingMessage",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingMessage.created_at",
    )
    reviews = relationship(
        "BookingReview",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingReview.created_at",
    )
    complaint = relationship(
        "BookingComplaint",
        back_populates="booking",
        uselist=False,
        cascade="all, delete-orphan",
    )


class BookingMessage(Base):
    __tablename__ = "booking_message"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("booking.id"), nullable=False)
    sender_user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="messages")
    sender = relationship("User")


class BookingReview(Base):
    __tablename__ = "booking_review"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_booking_review_rating_range"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id"),
        nullable=False,
        index=True,
    )
    author_user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True)
    guest_name = Column(String, nullable=True)
    rating = Column(Integer, nullable=False)  # 1-5
    emoji = Column(String, nullable=True)
    comment = Column(Text, nullable=True)
    photo_urls = Column(JSONB, nullable=True)
    video_urls = Column(JSONB, nullable=True)
    # Final contractor review of the show (counts toward musician rating).
    is_final = Column(Boolean, nullable=False, default=False)
    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking", back_populates="reviews")
    author = relationship("User")
