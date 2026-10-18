
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base

class PayoutRequest(Base):
    __tablename__ = 'payout_request'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    musician_id = Column(UUID(as_uuid=True), ForeignKey('musician_profile.id'), nullable=False)
    amount = Column(Numeric, nullable=False)
    status = Column(String, default='pending', nullable=False) # pending, processed, rejected
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
