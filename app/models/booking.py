import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    String,
    Date,
    Time,
    Numeric,
    Enum,
    DateTime,
    ForeignKey,
    Text,
    Integer,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db.session import Base
import enum


class BookingStatus(str, enum.Enum):
    requested = "requested"
    accepted = "accepted"
    contract_pending = "contract_pending"
    contract_signed = "contract_signed"
    payment_pending = "payment_pending"
    payment_retained = "payment_retained"  # Reserva confirmada (anticipo validado)
    change_pending = "change_pending"
    balance_pending = "balance_pending"
    balance_review = "balance_review"
    in_progress = "in_progress"
    payment_released = "payment_released"
    completed = "completed"
    cancelled = "cancelled"


class Booking(Base):
    __tablename__ = "booking"

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

    price_agreed = Column(Numeric, nullable=True)
    advance_amount = Column(Numeric, nullable=True)
    # Snapshot: comisión de plataforma sobre price_agreed (la paga el contratista).
    platform_fee_percent = Column(Numeric, nullable=True)
    platform_fee_amount = Column(Numeric, nullable=True)

    musician_quote_notes = Column(String, nullable=True)
    quoted_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)
    cancelled_by = Column(String, nullable=True)

    # Pending event changes proposed by either party (awaiting accept/reject)
    pending_location_address = Column(String, nullable=True)
    pending_location_city = Column(String, nullable=True)
    pending_location_reference = Column(String, nullable=True)
    pending_event_description = Column(String, nullable=True)
    pending_change_notes = Column(String, nullable=True)
    pending_price_agreed = Column(Numeric, nullable=True)
    pending_advance_amount = Column(Numeric, nullable=True)
    change_requested_by = Column(String, nullable=True)  # "contractor" | "musician"
    change_requested_at = Column(DateTime, nullable=True)

    # Integraciones externas
    calendar_event_id = Column(String, nullable=True)

    # Guest share link (enabled after final balance is covered)
    share_token = Column(String, unique=True, nullable=True, index=True)
    share_enabled = Column(Boolean, nullable=False, default=False)
    share_enabled_at = Column(DateTime, nullable=True)

    status = Column(Enum(BookingStatus),
                    default=BookingStatus.requested,
                    nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
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
    created_at = Column(DateTime, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="messages")
    sender = relationship("User")


class BookingReview(Base):
    __tablename__ = "booking_review"

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking", back_populates="reviews")
    author = relationship("User")
