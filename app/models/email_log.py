import uuid
from datetime import datetime

from sqlalchemy import Column, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.session import Base
from app.db.types import UTCDateTime


class EmailLog(Base):
    __tablename__ = "email_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_slug = Column(String, nullable=False, index=True)
    recipient = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=False)
    status = Column(String, nullable=False, index=True)  # sent | failed | skipped
    error_message = Column(Text, nullable=True)
    provider_message_id = Column(String, nullable=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True, index=True)
    meta = Column(JSONB, nullable=True)
    created_at = Column(UTCDateTime, default=datetime.utcnow, index=True)
