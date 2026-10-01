import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, Enum, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
import enum


class UserRole(str, enum.Enum):
    contractor = "contractor"
    musician = "musician"
    admin = "admin"


class User(Base):
    __tablename__ = "user"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)
    fullname = Column(String, nullable=True)

    role = Column(Enum(UserRole), nullable=False)

    phone = Column(String, nullable=True)
    profile_picture_url = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True, nullable=False)

    email_verified_at = Column(DateTime, nullable=True)
    email_verification_token = Column(String, nullable=True, unique=True, index=True)
    email_verification_expires_at = Column(DateTime, nullable=True)
    password_reset_token = Column(String, nullable=True, unique=True, index=True)
    password_reset_expires_at = Column(DateTime, nullable=True)

    terms_accepted_at = Column(DateTime, nullable=True)
    terms_accepted_ip = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    # Se incrementa para invalidar todas las sesiones (cambio/reset de contraseña,
    # vinculación de cuenta social por email, "cerrar sesión en todos lados").
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    
    google_calendar_refresh_token = Column(String, nullable=True)

    musician_profile = relationship(
        "MusicianProfile", back_populates="user", uselist=False
    )

    contractor_profile = relationship(
        "ContractorProfile", back_populates="user", uselist=False
    )

    oauth_accounts = relationship(
        "OAuthAccount",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def registration_type(self) -> str:
        if self.oauth_accounts:
            return str(self.oauth_accounts[0].provider.value)
        return "chivapp"

