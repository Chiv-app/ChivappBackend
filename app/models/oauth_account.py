import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class OAuthProvider(str, enum.Enum):
    google = "google"
    facebook = "facebook"


class OAuthAccount(Base):
    __tablename__ = "oauth_account"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_user"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(Enum(OAuthProvider), nullable=False)
    provider_user_id = Column(String, nullable=False)
    email = Column(String, nullable=True)
    created_at = Column(UTCDateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="oauth_accounts")
