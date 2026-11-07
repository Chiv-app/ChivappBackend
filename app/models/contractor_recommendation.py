import uuid
from datetime import datetime

from sqlalchemy import Column, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class ContractorRecommendation(Base):
    __tablename__ = "contractor_recommendation"
    __table_args__ = (
        UniqueConstraint("booking_id", name="uq_contractor_recommendation_booking"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    musician_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contractor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("contractor_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating = Column(Integer, nullable=False)  # 1-5
    comment = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, default=datetime.utcnow)

    booking = relationship("Booking")
    musician = relationship("MusicianProfile")
    contractor = relationship("ContractorProfile")
