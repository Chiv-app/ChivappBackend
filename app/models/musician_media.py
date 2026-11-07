import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Enum, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime
import enum


class MediaType(str, enum.Enum):
    image = "image"
    video = "video"
    audio = "audio"


class MusicianMedia(Base):
    __tablename__ = "musician_media"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    musician_id = Column(
        UUID(as_uuid=True),
        ForeignKey("musician_profile.id"),
        nullable=False
    )

    type = Column(Enum(MediaType), nullable=False)
    url = Column(String, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    order_index = Column(Integer, default=0)

    created_at = Column(UTCDateTime, default=datetime.utcnow)

    musician = relationship("MusicianProfile")
