import uuid
from datetime import datetime, time

from sqlalchemy import CheckConstraint, Column, Integer, Time, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class MusicianAvailability(Base):
    __tablename__ = "musician_availability"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_musician_availability_day_range"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    musician_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id"),
        nullable=False
    )

    # 0 = domingo, 1 = lunes, ..., 6 = sábado (misma convención que JS Date.getDay)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)

    created_at = Column(UTCDateTime, default=datetime.utcnow)

    musician = relationship("MusicianProfile")
