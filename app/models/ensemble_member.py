import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship

from app.db.session import Base
from app.db.types import UTCDateTime


class EnsembleMemberStatus(str, enum.Enum):
    invited = "invited"
    active = "active"
    inactive = "inactive"


class BookingMemberInviteStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"


class BookingMemberPayoutStatus(str, enum.Enum):
    draft = "draft"
    locked = "locked"
    paid = "paid"


class EnsembleMember(Base):
    """Integrante de la agrupación de un músico líder."""

    __tablename__ = "ensemble_member"
    __table_args__ = (
        UniqueConstraint(
            "leader_user_id",
            "email",
            name="uq_ensemble_member_leader_email",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    leader_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    email = Column(String, nullable=False, index=True)
    fullname = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    specialties = Column(ARRAY(String), nullable=False, default=list)
    notes = Column(Text, nullable=True)

    status = Column(
        Enum(EnsembleMemberStatus, name="ensemblememberstatus", create_constraint=False),
        nullable=False,
        default=EnsembleMemberStatus.invited,
    )

    password_setup_token = Column(String, nullable=True, unique=True, index=True)
    password_setup_expires_at = Column(UTCDateTime, nullable=True)
    invited_at = Column(UTCDateTime, default=datetime.utcnow)
    joined_at = Column(UTCDateTime, nullable=True)
    # Quién terminó la relación: 'leader' (lo dio de baja) o 'member' (rechazó o se salió).
    ended_by = Column(String(16), nullable=True)
    ended_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    leader = relationship("User", foreign_keys=[leader_user_id])
    member_user = relationship("User", foreign_keys=[member_user_id])


class BookingMemberInvite(Base):
    """Convocatoria de un integrante a un evento (opción A: RSVP por link)."""

    __tablename__ = "booking_member_invite"
    __table_args__ = (
        UniqueConstraint(
            "booking_id",
            "ensemble_member_id",
            name="uq_booking_member_invite",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ensemble_member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ensemble_member.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status = Column(
        Enum(
            BookingMemberInviteStatus,
            name="bookingmemberinvitestatus",
            create_constraint=False,
        ),
        nullable=False,
        default=BookingMemberInviteStatus.pending,
    )
    response_token = Column(String, nullable=False, unique=True, index=True)
    invited_at = Column(UTCDateTime, default=datetime.utcnow)
    responded_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking")
    ensemble_member = relationship("EnsembleMember")


class BookingMemberPayout(Base):
    """Reparto de paga definido por el líder para un integrante en un evento."""

    __tablename__ = "booking_member_payout"
    __table_args__ = (
        UniqueConstraint(
            "booking_id",
            "ensemble_member_id",
            name="uq_booking_member_payout",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(
        UUID(as_uuid=True),
        ForeignKey("booking.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ensemble_member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ensemble_member.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    amount = Column(Numeric(10, 2), nullable=False, default=0)
    currency = Column(String, nullable=False, default="PEN")
    status = Column(
        Enum(
            BookingMemberPayoutStatus,
            name="bookingmemberpayoutstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=BookingMemberPayoutStatus.draft,
    )
    note = Column(Text, nullable=True)
    set_by_leader_at = Column(UTCDateTime, nullable=True)
    paid_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("Booking")
    ensemble_member = relationship("EnsembleMember")
