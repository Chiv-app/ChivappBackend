"""Ubicación en vivo durante la fase de evento (multi-participante)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class BookingLocationShare(Base):
    """Snapshot del evento y contenedor de sesión por reserva."""

    __tablename__ = "booking_location_share"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Legacy binary columns (kept for older DBs; unused by multi-participant flow).
    musician_sharing = Column(Boolean, nullable=False, default=False)
    contractor_sharing = Column(Boolean, nullable=False, default=False)
    musician_requested_at = Column(UTCDateTime, nullable=True)
    contractor_requested_at = Column(UTCDateTime, nullable=True)
    musician_requested_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    contractor_requested_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    musician_lat = Column(Float, nullable=True)
    musician_lng = Column(Float, nullable=True)
    musician_accuracy = Column(Float, nullable=True)
    musician_updated_at = Column(UTCDateTime, nullable=True)
    contractor_lat = Column(Float, nullable=True)
    contractor_lng = Column(Float, nullable=True)
    contractor_accuracy = Column(Float, nullable=True)
    contractor_updated_at = Column(UTCDateTime, nullable=True)

    event_lat = Column(Float, nullable=True)
    event_lng = Column(Float, nullable=True)
    event_address = Column(String, nullable=True)
    event_city = Column(String, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking")


class BookingLocationParticipant(Base):
    """Estado de ubicación por usuario (líder, integrante o contratista)."""

    __tablename__ = "booking_location_participant"
    __table_args__ = (
        UniqueConstraint(
            "booking_id",
            "user_id",
            name="uq_booking_location_participant_booking_user",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # leader | member | contractor
    role = Column(String, nullable=False)
    display_name = Column(String, nullable=False, default="")

    sharing = Column(Boolean, nullable=False, default=False)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    accuracy = Column(Float, nullable=True)
    updated_at = Column(UTCDateTime, nullable=True)

    requested_at = Column(UTCDateTime, nullable=True)
    requested_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    created_at = Column(UTCDateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class BookingLocationPing(Base):
    """Historial de pings/acciones de ubicación para análisis posterior."""

    __tablename__ = "booking_location_ping"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # leader | member | contractor | musician (legacy)
    party = Column(String, nullable=False)
    # share_start | update | refresh | stop | request | accept | stale_stop
    action = Column(String, nullable=False)

    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    accuracy = Column(Float, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow, index=True)

    booking = relationship("Booking")
