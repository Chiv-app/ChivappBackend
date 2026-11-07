import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, Integer, Text,
    ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.db.session import Base
from app.db.types import UTCDateTime


class Contract(Base):
    __tablename__ = "contract"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    booking_id = Column(UUID(as_uuid=True),
                        ForeignKey("booking.id"),
                        unique=True)

    # Snapshot inmutable del contrato en la solicitud (fuente de verdad).
    title = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    context = Column(JSONB, nullable=True)

    # Legacy: PDFs persistidos en disco (solo lectura para contratos antiguos).
    contract_pdf_url = Column(String, nullable=True)
    contract_signed_pdf_url = Column(String, nullable=True)
    contractor_signature_url = Column(String, nullable=True)
    # Firma del músico congelada desde su perfil al crear la contrata.
    musician_signature_url = Column(String, nullable=True)

    terms_version = Column(Integer, default=1)

    contractor_signed = Column(Boolean, default=False)
    musician_signed = Column(Boolean, default=False)

    terms_accepted = Column(Boolean, default=False)
    terms_accepted_at = Column(UTCDateTime, nullable=True)
    terms_accepted_ip = Column(String, nullable=True)

    contractor_sign_timestamp = Column(UTCDateTime, nullable=True)
    musician_sign_timestamp = Column(UTCDateTime, nullable=True)

    contractor_sign_ip = Column(String, nullable=True)
    musician_sign_ip = Column(String, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    booking = relationship("Booking")
