import uuid
import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, String, Date, Time, Numeric, Enum, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime

class BookingLinkStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    expired = "expired"

class BookingLink(Base):
    __tablename__ = "booking_link"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_booking_link_price_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String, unique=True, index=True, nullable=False)
    
    musician_id = Column(UUID(as_uuid=True), ForeignKey("musician_profile.id"), nullable=False)
    price = Column(Numeric(12, 2), nullable=False)
    
    event_date = Column(Date, nullable=True)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    event_type = Column(String(120), nullable=True)
    location_address = Column(String(300), nullable=True)
    location_city = Column(String(120), nullable=True)
    location_reference = Column(String(255), nullable=True)
    client_fullname = Column(String(120), nullable=True)
    client_phone = Column(String(40), nullable=True)
    client_email = Column(String, nullable=True)
    client_document_number = Column(String(20), nullable=True)
    event_description = Column(Text, nullable=True)
    source = Column(String(32), nullable=False, default="manual", server_default=text("'manual'"))
    
    status = Column(Enum(BookingLinkStatus), default=BookingLinkStatus.active, nullable=False)
    expires_at = Column(UTCDateTime, nullable=False)
    
    # Once completed, it creates a real booking
    booking_id = Column(UUID(as_uuid=True), ForeignKey("booking.id"), nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    musician = relationship("MusicianProfile")
    booking = relationship("Booking")
