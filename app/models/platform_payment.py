import uuid
from datetime import datetime

from sqlalchemy import Column, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base
from app.db.types import UTCDateTime


class PlatformPaymentSettings(Base):
    """Singleton-style row: destination account for contractor deposits."""

    __tablename__ = "platform_payment_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String, nullable=False, default="")
    phone_label = Column(String, nullable=False, default="Yape / Plin")
    account_name = Column(String, nullable=True)
    qr_image_url = Column(String, nullable=True)
    instructions = Column(Text, nullable=True)
    # Percent added on top of musician price; only the contractor pays it.
    platform_fee_percent = Column(Numeric(5, 2), nullable=False, default=2)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(UTCDateTime, default=datetime.utcnow)
