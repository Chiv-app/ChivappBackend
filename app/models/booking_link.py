import uuid
import enum
from datetime import datetime

from sqlalchemy import Column, String, Date, Time, Numeric, DateTime, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base

class BookingLinkStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    expired = "expired"

class BookingLink(Base):
    __tablename__ = "booking_link"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String, unique=True, index=True, nullable=False)
    
    musician_id = Column(UUID(as_uuid=True), ForeignKey("musician_profile.id"), nullable=False)
    price = Column(Numeric, nullable=False)
    
    event_date = Column(Date, nullable=True)
    start_time = Column(Time, nullable=True)
    client_email = Column(String, nullable=True)
    
    status = Column(Enum(BookingLinkStatus), default=BookingLinkStatus.active, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    
    # Once completed, it creates a real booking
    booking_id = Column(UUID(as_uuid=True), ForeignKey("booking.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    musician = relationship("MusicianProfile")
    booking = relationship("Booking")
