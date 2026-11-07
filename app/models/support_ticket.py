import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class SupportTicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"


class SupportTicket(Base):
    __tablename__ = "support_ticket"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True, index=True)

    # Solo se usan cuando no hay sesión (visitante anónimo).
    guest_name = Column(String, nullable=True)
    guest_email = Column(String, nullable=True)

    message = Column(Text, nullable=False)
    status = Column(
        Enum(SupportTicketStatus, name="supportticketstatus"),
        nullable=False,
        default=SupportTicketStatus.open,
    )

    admin_response = Column(Text, nullable=True)
    responded_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=True)
    responded_at = Column(UTCDateTime, nullable=True)

    # Metadata de la operación puntual — clave para trazar tickets anónimos.
    submitted_ip = Column(String, nullable=True)
    submitted_user_agent = Column(String, nullable=True)
    submitted_path = Column(String, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])
    responded_by = relationship("User", foreign_keys=[responded_by_admin_id])
