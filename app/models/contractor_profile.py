import uuid
from datetime import datetime

from sqlalchemy import Column, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime
from app.models.profile_status import ProfileStatus


class ContractorProfile(Base):
    __tablename__ = "contractor_profile"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), unique=True)

    bio = Column(String, nullable=True)
    preferences = Column(ARRAY(String), nullable=True)

    document_type = Column(String, nullable=True)
    document_number = Column(String, nullable=True)
    address = Column(String, nullable=True)
    city = Column(String, nullable=True)
    id_document_url = Column(String, nullable=True)

    contract_template_title = Column(String, nullable=True)
    contract_template_body = Column(String, nullable=True)
    contract_pdf_url = Column(String, nullable=True)

    status = Column(
        Enum(ProfileStatus),
        nullable=False,
        default=ProfileStatus.draft,
    )
    submitted_at = Column(UTCDateTime, nullable=True)
    published_at = Column(UTCDateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)

    rating_avg = Column(Numeric(2, 1), nullable=True)
    rating_count = Column(Integer, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="contractor_profile")

    @property
    def username(self) -> str | None:
        return self.user.username if self.user else None

    @property
    def fullname(self) -> str | None:
        return self.user.fullname if self.user else None

    @property
    def email(self) -> str | None:
        return self.user.email if self.user else None

    @property
    def phone(self) -> str | None:
        return self.user.phone if self.user else None
