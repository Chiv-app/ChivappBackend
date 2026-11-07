import uuid
from datetime import datetime

from sqlalchemy import Column, String, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class Notification(Base):
    __tablename__ = "notification"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=False)

    type = Column(String, nullable=False)      # ej: 'booking_created', 'payment_released'
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)

    meta = Column(JSONB, nullable=True)    # ej: {"booking_id": "..."}
    is_read = Column(Boolean, default=False)

    created_at = Column(UTCDateTime, default=datetime.utcnow)

    user = relationship("User")
