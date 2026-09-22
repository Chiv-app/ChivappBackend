import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, Boolean
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.models.profile_status import ProfileStatus
import enum


class AvailabilityType(str, enum.Enum):
    hourly = "hourly"
    per_event = "per_event"
    both = "both"


class MusicianProfile(Base):
    __tablename__ = "musician_profile"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    is_ensemble_only = Column(Boolean, default=False, nullable=False)

    stage_name = Column(String, nullable=True)
    # Slug único derivado de stage_name, para URLs públicas legibles
    # (/musicians/{slug}). Se recalcula cada vez que cambia stage_name.
    slug = Column(String, unique=True, index=True, nullable=True)
    bio = Column(String, nullable=True)

    genres = Column(ARRAY(String), nullable=False, default=list)
    instruments = Column(ARRAY(String), nullable=False, default=list)
    songs = Column(ARRAY(String), nullable=False, default=list)
    # [{ "title": str, "youtube_url": str | null }]
    repertoire = Column(JSONB, nullable=False, default=list)

    price_per_hour = Column(Numeric(10, 2), nullable=True)
    price_per_event = Column(Numeric(10, 2), nullable=True)

    portfolio_description = Column(String, nullable=True)

    location_city = Column(String)
    location_zone = Column(String)

    availability_type = Column(
        Enum(AvailabilityType),
        nullable=False,
        default=AvailabilityType.both,
    )

    profile_image_url = Column(String)
    gallery_images = Column(ARRAY(String))
    videos = Column(ARRAY(String))
    # Optional featured video for the public profile showreel; defaults to videos[0].
    showreel_video_url = Column(String, nullable=True)

    instagram_url = Column(String, nullable=True)
    facebook_url = Column(String, nullable=True)
    tiktok_url = Column(String, nullable=True)
    youtube_channel_url = Column(String, nullable=True)
    spotify_url = Column(String, nullable=True)
    website_url = Column(String, nullable=True)

    id_document_url = Column(String, nullable=True)

    contract_template_title = Column(String, nullable=True)
    contract_template_body = Column(Text, nullable=True)
    contract_pdf_url = Column(String, nullable=True)
    # Firma digital del músico (configurada en perfil; alimenta sus contratas).
    signature_image_url = Column(String, nullable=True)

    # Datos para cobro / desembolsos (Opción 2: Transferencias / Mercado Pago / Yape / Plin)
    payout_method = Column(String, nullable=True)  # "bank_transfer", "yape_plin", "mercadopago"
    payout_bank_name = Column(String, nullable=True)  # "BCP", "BBVA", "Interbank", etc.
    payout_account_number = Column(String, nullable=True)
    payout_cci = Column(String, nullable=True)
    payout_phone = Column(String, nullable=True)
    payout_beneficiary_name = Column(String, nullable=True)
    payout_beneficiary_document = Column(String, nullable=True)
    payout_mp_email = Column(String, nullable=True)

    status = Column(

        Enum(ProfileStatus),
        nullable=False,
        default=ProfileStatus.draft,
    )
    submitted_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)

    rating_avg = Column(Numeric(2, 1), nullable=True)
    rating_count = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="musician_profile")

    @property
    def username(self) -> str | None:
        return self.user.username if self.user else None

    @property
    def fullname(self) -> str | None:
        return self.user.fullname if self.user else None

    @property
    def email(self) -> str | None:
        return self.user.email if self.user else None
