from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models.booking import Booking
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.schemas.booking import BookingOut

BookingViewerRole = Literal["owner", "member"]


def booking_parties_load_options():
    return (
        joinedload(Booking.musician),
        joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        joinedload(Booking.complaint),
    )


def serialize_booking_out(
    booking: Booking,
    *,
    viewer_role: BookingViewerRole | None = None,
    member_invite_status: str | None = None,
) -> BookingOut:
    """Map ORM booking + optional party preview fields."""
    base = BookingOut.model_validate(booking)
    musician = booking.musician
    contractor = booking.contractor
    contractor_user = contractor.user if contractor else None
    complaint = getattr(booking, "complaint", None)
    # Un integrante convocado ve el evento (fecha, lugar, estado) pero no el dinero ni las disputas
    # entre el líder y el contratante.
    private_money: dict = (
        {
            "price_agreed": None,
            "platform_fee_percent": None,
            "platform_fee_amount": None,
            "musician_quote_notes": None,
            "rejection_reason": None,
            "cancellation_refund_percent": None,
            "cancellation_refund_amount": None,
            "cancellation_refund_status": None,
            "pending_price_agreed": None,
        }
        if viewer_role == "member"
        else {}
    )
    complaint = None if viewer_role == "member" else complaint
    return base.model_copy(
        update={
            **private_money,
            "musician_name": musician.stage_name if musician else None,
            "musician_image_url": (
                musician.profile_image_url if musician else None
            ),
            "contractor_name": (
                contractor_user.fullname if contractor_user else None
            ),
            "contractor_image_url": (
                contractor_user.profile_picture_url if contractor_user else None
            ),
            "viewer_role": viewer_role,
            "member_invite_status": member_invite_status,
            "complaint_status": (
                complaint.status.value if complaint and complaint.status else None
            ),
            "complaint_reason": complaint.reason if complaint else None,
        }
    )


def parse_uuid(value: str, label: str = "id") -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} inválido",
        ) from exc


def get_booking_or_404(db: Session, booking_id: str) -> Booking:
    booking = db.get(Booking, parse_uuid(booking_id, "booking_id"))
    if not booking:
        raise HTTPException(status_code=404, detail="Booking no encontrado")
    return booking


def get_contractor_profile(
    db: Session,
    user: User,
) -> ContractorProfile | None:
    if user.role != UserRole.contractor:
        return None
    return (
        db.query(ContractorProfile)
        .filter(ContractorProfile.user_id == user.id)
        .first()
    )


def get_musician_profile(
    db: Session,
    user: User,
) -> MusicianProfile | None:
    if user.role != UserRole.musician:
        return None
    return (
        db.query(MusicianProfile)
        .filter(MusicianProfile.user_id == user.id)
        .first()
    )


def get_contractor_profile_or_400(
    db: Session,
    user: User,
    *,
    require_verified: bool = False,
) -> ContractorProfile:
    if user.role != UserRole.contractor:
        raise HTTPException(
            status_code=403,
            detail="Solo los contratistas pueden realizar esta acción",
        )
    contractor = get_contractor_profile(db, user)
    if not contractor:
        contractor = ContractorProfile(
            user_id=user.id,
            status=ProfileStatus.draft,
        )
        db.add(contractor)
        db.commit()
        db.refresh(contractor)
    if require_verified and (contractor.status != ProfileStatus.published or not user.is_verified):
        raise HTTPException(
            status_code=403,
            detail="Tu perfil de contratista debe estar verificado para esta acción",
        )
    return contractor


def get_musician_profile_or_400(
    db: Session,
    user: User,
    *,
    require_verified: bool = False,
) -> MusicianProfile:
    musician = get_musician_profile(db, user)
    if not musician:
        raise HTTPException(
            status_code=400,
            detail="Debes tener un perfil de músico creado",
        )
    if require_verified and (
        musician.status != ProfileStatus.published or not user.is_verified
    ):
        raise HTTPException(
            status_code=403,
            detail="Tu perfil de músico debe estar verificado para esta acción",
        )
    return musician


def get_booking_member_invite(
    db: Session,
    booking: Booking,
    user: User,
    *,
    statuses: list[BookingMemberInviteStatus] | None = None,
) -> BookingMemberInvite | None:
    """Invite linking the user as ensemble member of this booking (if any)."""
    if user.role != UserRole.musician:
        return None
    allowed = statuses or [
        BookingMemberInviteStatus.pending,
        BookingMemberInviteStatus.accepted,
    ]
    return (
        db.query(BookingMemberInvite)
        .join(
            EnsembleMember,
            EnsembleMember.id == BookingMemberInvite.ensemble_member_id,
        )
        .filter(
            BookingMemberInvite.booking_id == booking.id,
            EnsembleMember.member_user_id == user.id,
            # Un integrante dado de baja pierde el acceso a las reservas a las que estaba convocado.
            EnsembleMember.status != EnsembleMemberStatus.inactive,
            BookingMemberInvite.status.in_(allowed),
        )
        .first()
    )


def get_accepted_booking_member_invite(
    db: Session,
    booking: Booking,
    user: User,
) -> BookingMemberInvite | None:
    return get_booking_member_invite(
        db,
        booking,
        user,
        statuses=[BookingMemberInviteStatus.accepted],
    )


def resolve_member_invite_status(
    db: Session,
    booking: Booking,
    user: User,
) -> str | None:
    """Pending/accepted/declined invite status for the current musician, if any."""
    invite = get_booking_member_invite(
        db,
        booking,
        user,
        statuses=[
            BookingMemberInviteStatus.pending,
            BookingMemberInviteStatus.accepted,
            BookingMemberInviteStatus.declined,
        ],
    )
    return invite.status.value if invite else None


def list_member_booking_ids(db: Session, user: User) -> list[UUID]:
    """Booking IDs where the user was convocated as ensemble member."""
    if user.role != UserRole.musician:
        return []
    rows = (
        db.query(BookingMemberInvite.booking_id)
        .join(
            EnsembleMember,
            EnsembleMember.id == BookingMemberInvite.ensemble_member_id,
        )
        .filter(
            EnsembleMember.member_user_id == user.id,
            EnsembleMember.status != EnsembleMemberStatus.inactive,
            BookingMemberInvite.status.in_(
                [
                    BookingMemberInviteStatus.pending,
                    BookingMemberInviteStatus.accepted,
                ]
            ),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def resolve_booking_viewer_role(
    db: Session,
    booking: Booking,
    user: User,
) -> BookingViewerRole | None:
    contractor = get_contractor_profile(db, user)
    if contractor and booking.contractor_id == contractor.id:
        return "owner"

    musician = get_musician_profile(db, user)
    if musician and booking.musician_id == musician.id:
        return "owner"

    if get_booking_member_invite(db, booking, user):
        return "member"

    return None


def assert_booking_participant(
    db: Session,
    booking: Booking,
    user: User,
) -> None:
    """Contractor or lead musician of the booking (can act as owner)."""
    contractor = get_contractor_profile(db, user)
    if contractor and booking.contractor_id == contractor.id:
        return

    musician = get_musician_profile(db, user)
    if musician and booking.musician_id == musician.id:
        return

    raise HTTPException(status_code=403, detail="No autorizado")


def assert_booking_collaborator(
    db: Session,
    booking: Booking,
    user: User,
) -> None:
    """
    Owner parties or accepted ensemble member.
    Used for chat, live location and guest share.
    """
    try:
        assert_booking_participant(db, booking, user)
        return
    except HTTPException:
        pass

    if get_accepted_booking_member_invite(db, booking, user):
        return

    raise HTTPException(status_code=403, detail="No autorizado")


def assert_booking_viewer(
    db: Session,
    booking: Booking,
    user: User,
) -> BookingViewerRole:
    """Owner parties or associated ensemble member (read access)."""
    role = resolve_booking_viewer_role(db, booking, user)
    if role is None:
        raise HTTPException(status_code=403, detail="No autorizado")
    return role


def assert_booking_contractor_owner(
    db: Session,
    booking: Booking,
    user: User,
) -> ContractorProfile:
    contractor = get_contractor_profile_or_400(db, user)
    if booking.contractor_id != contractor.id:
        raise HTTPException(status_code=403, detail="No autorizado")
    return contractor


def assert_booking_musician_owner(
    db: Session,
    booking: Booking,
    user: User,
) -> MusicianProfile:
    musician = get_musician_profile_or_400(db, user)
    if booking.musician_id != musician.id:
        raise HTTPException(status_code=403, detail="No autorizado")
    return musician
