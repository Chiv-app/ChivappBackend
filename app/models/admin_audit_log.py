import uuid
from datetime import datetime

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)
    target_id = Column(String, nullable=True, index=True)
    before_data = Column(JSONB, nullable=True)
    after_data = Column(JSONB, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=datetime.utcnow, index=True)

    admin_user = relationship("User", lazy="joined")

    @property
    def admin_email(self) -> str | None:
        return self.admin_user.email if self.admin_user else None

    @property
    def before_state(self) -> dict | None:
        return self.before_data

    @property
    def after_state(self) -> dict | None:
        return self.after_data
