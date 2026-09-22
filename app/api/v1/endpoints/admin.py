from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.api.booking_helpers import parse_uuid
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.models.support_ticket import SupportTicket, SupportTicketStatus
from app.models.user import User, UserRole
from app.schemas.admin import (
    AdminActivityItem,
    AdminBookingCancel,
    AdminBookingDetailOut,
    AdminBookingOut,
    AdminPaymentOut,
    AdminPaymentReject,
    AdminPaymentReviewItem,
    AdminProfileStatusUpdate,
    AdminSettleBooking,
    AdminStatsOut,
    AdminUserOut,
    AdminUserUpdate,
)
from app.schemas.contract import ContractOut
from app.schemas.payment import PaymentOut
from app.schemas.support import AdminSupportTicketOut, SupportTicketRespond
from app.schemas.settlement import (
    AdminRefundTransfer,
    AdminReleaseSettlement,
    AdminSettlementOut,
    MusicianPayoutInfoOut,
    PlatformPaymentInstructionsOut,
    PlatformPaymentInstructionsUpdate,
)

from app.schemas.profiles import (
    ContractorProfileAdminOut,
    MusicianProfileAdminOut,
    ProfileReviewAction,
)
from app.services import payment_review
from app.services.booking_notifications import (
    notify_balance_rejected,
    notify_balance_validated,
    notify_payment_rejected,
    notify_payment_validated,
    notify_profile_approved,
    notify_profile_needs_resubmit,
    notify_profile_rejected,
)
from app.services.payment_evidence import payment_evidence_list

router = APIRouter(prefix="/admin", tags=["Admin"])

ACTIVE_BOOKING_STATUSES = {
    BookingStatus.requested,
    BookingStatus.accepted,
    BookingStatus.contract_pending,
    BookingStatus.contract_signed,
    BookingStatus.payment_pending,
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.balance_pending,
    BookingStatus.balance_review,
    BookingStatus.in_progress,
}

PAYMENT_REVIEW_STATUSES = {
    BookingStatus.payment_pending,
    BookingStatus.balance_review,
}


def _musician_admin_out(profile: MusicianProfile) -> MusicianProfileAdminOut:
    out = MusicianProfileAdminOut.model_validate(profile, from_attributes=True)
    if profile.user:
        out.user_email = profile.user.email
        out.user_fullname = profile.user.fullname
        out.user_phone = profile.user.phone
    return out


def _contractor_admin_out(profile: ContractorProfile) -> ContractorProfileAdminOut:
    out = ContractorProfileAdminOut.model_validate(profile, from_attributes=True)
    if profile.user:
        out.user_email = profile.user.email
        out.user_fullname = profile.user.fullname
        out.user_phone = profile.user.phone
    return out


def _booking_admin_out(booking: Booking) -> AdminBookingOut:
    musician = booking.musician
    contractor = booking.contractor
    return AdminBookingOut(
        id=booking.id,
        status=booking.status.value if hasattr(booking.status, "value") else str(booking.status),
        event_type=booking.event_type,
        event_date=booking.event_date,
        start_time=booking.start_time,
        location_address=booking.location_address,
        location_city=booking.location_city,
        price_agreed=booking.price_agreed,
        advance_amount=booking.advance_amount,
        share_enabled=bool(booking.share_enabled),
        change_requested_by=booking.change_requested_by,
        musician_id=booking.musician_id,
        contractor_id=booking.contractor_id,
        musician_name=(
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else None)
        ),
        contractor_name=contractor.user.fullname if contractor and contractor.user else None,
        musician_email=musician.user.email if musician and musician.user else None,
        contractor_email=contractor.user.email if contractor and contractor.user else None,
        cancelled_by=booking.cancelled_by,
        rejection_reason=booking.rejection_reason,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
    )


def _payment_admin_out(payment: Payment) -> AdminPaymentOut:
    booking = payment.booking
    musician = booking.musician if booking else None
    contractor = booking.contractor if booking else None
    return AdminPaymentOut(
        id=payment.id,
        booking_id=payment.booking_id,
        amount=payment.amount,
        currency=payment.currency or "PEN",
        payment_type=payment.payment_type,
        evidence_url=payment.evidence_url,
        evidence_urls=payment_evidence_list(payment),
        status=payment.status.value if hasattr(payment.status, "value") else str(payment.status),
        retained_at=payment.retained_at,
        released_at=payment.released_at,
        reviewed_by_user_id=payment.reviewed_by_user_id,
        reviewed_at=payment.reviewed_at,
        rejection_reason=payment.rejection_reason,
        created_at=payment.created_at,
        event_type=booking.event_type if booking else None,
        event_date=booking.event_date if booking else None,
        musician_name=(
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else None)
        ),
        contractor_name=contractor.user.fullname if contractor and contractor.user else None,
    )


# ── Dashboard ───────────────────────────────────────────────────────────────


@router.get("/stats", response_model=AdminStatsOut)
def get_admin_stats(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    total_users = db.query(User).count()
    total_musicians = db.query(User).filter(User.role == UserRole.musician).count()
    total_contractors = db.query(User).filter(User.role == UserRole.contractor).count()
    pending_musician_profiles = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.status == ProfileStatus.pending_review)
        .count()
    )
    pending_contractor_profiles = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.status == ProfileStatus.pending_review)
        .count()
    )
    published_musicians = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.status == ProfileStatus.published)
        .count()
    )
    published_contractors = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.status == ProfileStatus.published)
        .count()
    )

    total_bookings = db.query(Booking).count()
    active_bookings = (
        db.query(Booking).filter(Booking.status.in_(ACTIVE_BOOKING_STATUSES)).count()
    )
    change_pending_bookings = (
        db.query(Booking).filter(Booking.status == BookingStatus.change_pending).count()
    )
    payment_review_bookings = (
        db.query(Booking).filter(Booking.status.in_(PAYMENT_REVIEW_STATUSES)).count()
    )
    completed_bookings = (
        db.query(Booking).filter(Booking.status == BookingStatus.completed).count()
    )
    cancelled_bookings = (
        db.query(Booking).filter(Booking.status == BookingStatus.cancelled).count()
    )
    share_enabled_bookings = (
        db.query(Booking).filter(Booking.share_enabled.is_(True)).count()
    )

    total_payments = db.query(Payment).count()
    retained_sum = (
        db.query(func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.status == PaymentStatus.retained)
        .scalar()
    )
    released_sum = (
        db.query(func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.status == PaymentStatus.released)
        .scalar()
    )

    return AdminStatsOut(
        total_users=total_users,
        total_musicians=total_musicians,
        total_contractors=total_contractors,
        pending_musician_profiles=pending_musician_profiles,
        pending_contractor_profiles=pending_contractor_profiles,
        published_musicians=published_musicians,
        published_contractors=published_contractors,
        total_bookings=total_bookings,
        active_bookings=active_bookings,
        change_pending_bookings=change_pending_bookings,
        payment_review_bookings=payment_review_bookings,
        completed_bookings=completed_bookings,
        cancelled_bookings=cancelled_bookings,
        total_payments=total_payments,
        retained_payments_amount=float(retained_sum or 0),
        released_payments_amount=float(released_sum or 0),
        share_enabled_bookings=share_enabled_bookings,
    )


# ── Users ───────────────────────────────────────────────────────────────────


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = Query(default=50, le=200),
    role: str | None = None,
    q: str | None = None,
    is_verified: bool | None = None,
    is_active: bool | None = None,
):
    query = db.query(User)
    if role:
        try:
            query = query.filter(User.role == UserRole(role))
        except ValueError as exc:
            raise HTTPException(400, "Rol inválido") from exc
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(User.email).like(term),
                func.lower(User.fullname).like(term),
            )
        )
    if is_verified is not None:
        query = query.filter(User.is_verified.is_(is_verified))
    if is_active is not None:
        query = query.filter(User.is_active.is_(is_active))
    return query.order_by(User.created_at.desc()).offset(skip).limit(limit).all()


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    current_admin: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    if user.id == current_admin.id and payload.is_active is False:
        raise HTTPException(400, "No puedes desactivar tu propia cuenta")
    if user.role == UserRole.admin and payload.is_active is False:
        raise HTTPException(400, "No se pueden desactivar cuentas admin desde aquí")

    if payload.is_verified is not None:
        user.is_verified = payload.is_verified
    if payload.is_active is not None:
        user.is_active = payload.is_active

    db.commit()
    db.refresh(user)
    return user


# ── Profiles ────────────────────────────────────────────────────────────────


@router.get("/musicians", response_model=list[MusicianProfileAdminOut])
def list_musicians(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    skip: int = 0,
    limit: int = Query(default=50, le=200),
):
    query = db.query(MusicianProfile).options(joinedload(MusicianProfile.user)).join(User)
    if status_filter:
        query = query.filter(MusicianProfile.status == status_filter)
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(MusicianProfile.stage_name).like(term),
                func.lower(User.email).like(term),
                func.lower(User.fullname).like(term),
            )
        )
    if status_filter == ProfileStatus.pending_review.value:
        query = query.order_by(MusicianProfile.submitted_at.asc())
    else:
        query = query.order_by(MusicianProfile.updated_at.desc())
    profiles = query.offset(skip).limit(limit).all()
    return [_musician_admin_out(profile) for profile in profiles]


@router.get("/contractors", response_model=list[ContractorProfileAdminOut])
def list_contractors(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    skip: int = 0,
    limit: int = Query(default=50, le=200),
):
    query = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .join(User)
    )
    if status_filter:
        query = query.filter(ContractorProfile.status == status_filter)
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            or_(
                func.lower(User.email).like(term),
                func.lower(User.fullname).like(term),
            )
        )
    if status_filter == ProfileStatus.pending_review.value:
        query = query.order_by(ContractorProfile.submitted_at.asc())
    else:
        query = query.order_by(ContractorProfile.updated_at.desc())
    profiles = query.offset(skip).limit(limit).all()
    return [_contractor_admin_out(profile) for profile in profiles]


@router.get("/musicians/pending", response_model=list[MusicianProfileAdminOut])
def list_pending_musicians(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profiles = (
        db.query(MusicianProfile)
        .options(joinedload(MusicianProfile.user))
        .join(User)
        .filter(MusicianProfile.status == ProfileStatus.pending_review)
        .order_by(MusicianProfile.submitted_at.desc())
        .all()
    )
    return [_musician_admin_out(profile) for profile in profiles]


@router.get("/contractors/pending", response_model=list[ContractorProfileAdminOut])
def list_pending_contractors(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profiles = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .join(User)
        .filter(ContractorProfile.status == ProfileStatus.pending_review)
        .order_by(ContractorProfile.submitted_at.desc())
        .all()
    )
    return [_contractor_admin_out(profile) for profile in profiles]


@router.get("/musicians/{musician_id}", response_model=MusicianProfileAdminOut)
def get_musician_admin_detail(
    musician_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = (
        db.query(MusicianProfile)
        .options(joinedload(MusicianProfile.user))
        .filter(MusicianProfile.id == musician_id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de músico no encontrado")
    return _musician_admin_out(profile)


@router.get("/contractors/{contractor_id}", response_model=ContractorProfileAdminOut)
def get_contractor_admin_detail(
    contractor_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .filter(ContractorProfile.id == contractor_id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de contratista no encontrado")
    return _contractor_admin_out(profile)


@router.post("/musicians/{musician_id}/approve", response_model=MusicianProfileAdminOut)
def approve_musician_profile(
    musician_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(MusicianProfile).filter(MusicianProfile.id == musician_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de músico no encontrado")
    if profile.status not in {ProfileStatus.pending_review, ProfileStatus.rejected}:
        raise HTTPException(status_code=400, detail="El perfil no puede aprobarse en este estado")

    profile.status = ProfileStatus.published
    profile.published_at = datetime.utcnow()
    profile.rejection_reason = None
    profile.user.is_verified = True
    notify_profile_approved(
        db,
        user=profile.user,
        profile_role="musician",
        profile_id=str(profile.id),
    )
    db.commit()
    db.refresh(profile)
    return _musician_admin_out(profile)


@router.post("/musicians/{musician_id}/reject", response_model=MusicianProfileAdminOut)
def reject_musician_profile(
    musician_id: str,
    payload: ProfileReviewAction,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(MusicianProfile).filter(MusicianProfile.id == musician_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de músico no encontrado")
    if profile.status not in {
        ProfileStatus.pending_review,
        ProfileStatus.published,
    }:
        raise HTTPException(status_code=400, detail="El perfil no puede rechazarse en este estado")

    profile.status = ProfileStatus.rejected
    profile.rejection_reason = payload.rejection_reason or "Perfil rechazado por el administrador."
    notify_profile_rejected(
        db,
        user=profile.user,
        profile_role="musician",
        profile_id=str(profile.id),
        reason=profile.rejection_reason,
    )
    db.commit()
    db.refresh(profile)
    return _musician_admin_out(profile)


@router.post("/musicians/{musician_id}/moderate", response_model=MusicianProfileAdminOut)
def moderate_musician_profile(
    musician_id: str,
    payload: AdminProfileStatusUpdate,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(MusicianProfile).filter(MusicianProfile.id == musician_id).first()
    if not profile:
        raise HTTPException(404, "Perfil de músico no encontrado")

    if payload.action == "unpublish":
        if profile.status != ProfileStatus.published:
            raise HTTPException(400, "Solo se pueden despublicar perfiles publicados")
        profile.status = ProfileStatus.draft
        profile.published_at = None
        profile.rejection_reason = payload.reason
    elif payload.action == "request_resubmit":
        profile.status = ProfileStatus.rejected
        profile.rejection_reason = (
            payload.reason or "Debes corregir y volver a enviar tu perfil."
        )
        notify_profile_needs_resubmit(
            db,
            user=profile.user,
            profile_role="musician",
            profile_id=str(profile.id),
        )

    db.commit()
    db.refresh(profile)
    return _musician_admin_out(profile)


@router.post("/contractors/{contractor_id}/approve", response_model=ContractorProfileAdminOut)
def approve_contractor_profile(
    contractor_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(ContractorProfile).filter(ContractorProfile.id == contractor_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de contratista no encontrado")
    if profile.status not in {ProfileStatus.pending_review, ProfileStatus.rejected}:
        raise HTTPException(status_code=400, detail="El perfil no puede aprobarse en este estado")

    profile.status = ProfileStatus.published
    profile.published_at = datetime.utcnow()
    profile.rejection_reason = None
    profile.user.is_verified = True
    notify_profile_approved(
        db,
        user=profile.user,
        profile_role="contractor",
        profile_id=str(profile.id),
    )
    db.commit()
    db.refresh(profile)
    return _contractor_admin_out(profile)


@router.post("/contractors/{contractor_id}/reject", response_model=ContractorProfileAdminOut)
def reject_contractor_profile(
    contractor_id: str,
    payload: ProfileReviewAction,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(ContractorProfile).filter(ContractorProfile.id == contractor_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Perfil de contratista no encontrado")
    if profile.status not in {
        ProfileStatus.pending_review,
        ProfileStatus.published,
    }:
        raise HTTPException(status_code=400, detail="El perfil no puede rechazarse en este estado")

    profile.status = ProfileStatus.rejected
    profile.rejection_reason = payload.rejection_reason or "Perfil rechazado por el administrador."
    notify_profile_rejected(
        db,
        user=profile.user,
        profile_role="contractor",
        profile_id=str(profile.id),
        reason=profile.rejection_reason,
    )
    db.commit()
    db.refresh(profile)
    return _contractor_admin_out(profile)


@router.post(
    "/contractors/{contractor_id}/moderate",
    response_model=ContractorProfileAdminOut,
)
def moderate_contractor_profile(
    contractor_id: str,
    payload: AdminProfileStatusUpdate,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    profile = db.query(ContractorProfile).filter(ContractorProfile.id == contractor_id).first()
    if not profile:
        raise HTTPException(404, "Perfil de contratista no encontrado")

    if payload.action == "unpublish":
        if profile.status != ProfileStatus.published:
            raise HTTPException(400, "Solo se pueden despublicar perfiles publicados")
        profile.status = ProfileStatus.draft
        profile.published_at = None
        profile.rejection_reason = payload.reason
    elif payload.action == "request_resubmit":
        profile.status = ProfileStatus.rejected
        profile.rejection_reason = (
            payload.reason or "Debes corregir y volver a enviar tu perfil."
        )
        notify_profile_needs_resubmit(
            db,
            user=profile.user,
            profile_role="contractor",
            profile_id=str(profile.id),
        )

    db.commit()
    db.refresh(profile)
    return _contractor_admin_out(profile)


# ── Bookings ops ────────────────────────────────────────────────────────────


@router.get("/bookings", response_model=list[AdminBookingOut])
def list_bookings(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
    skip: int = 0,
    limit: int = Query(default=50, le=200),
):
    query = db.query(Booking).options(
        joinedload(Booking.musician).joinedload(MusicianProfile.user),
        joinedload(Booking.contractor).joinedload(ContractorProfile.user),
    )
    if status_filter:
        try:
            query = query.filter(Booking.status == BookingStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(400, "Estado de reserva inválido") from exc
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = (
            query.join(MusicianProfile, Booking.musician_id == MusicianProfile.id)
            .join(User, MusicianProfile.user_id == User.id)
            .filter(
                or_(
                    func.lower(Booking.event_type).like(term),
                    func.lower(Booking.location_city).like(term),
                    func.lower(Booking.location_address).like(term),
                    func.lower(MusicianProfile.stage_name).like(term),
                    func.lower(User.email).like(term),
                )
            )
        )
    bookings = query.order_by(Booking.updated_at.desc()).offset(skip).limit(limit).all()
    return [_booking_admin_out(booking) for booking in bookings]


@router.get("/bookings/{booking_id}", response_model=AdminBookingOut)
def get_booking_admin(
    booking_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    return _booking_admin_out(booking)


@router.get("/bookings/{booking_id}/detail", response_model=AdminBookingDetailOut)
def get_booking_admin_detail(
    booking_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    """Vista completa de una reserva: contrato, historial de pagos, mensajes, queja/reseña."""
    from app.models.booking import BookingMessage, BookingReview
    from app.models.booking_complaint import BookingComplaint
    from app.models.contract import Contract
    from app.schemas.booking import BookingMessageOut
    from app.services.booking_lifecycle import remaining_balance, retained_paid_total
    from app.services.booking_share import serialize_review
    from app.services.settlement import serialize_complaint

    booking = _load_booking_with_parties(db, booking_id)
    base = _booking_admin_out(booking)

    musician = booking.musician
    contractor = booking.contractor

    due = remaining_balance(db, booking)
    paid = retained_paid_total(db, booking.id)

    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()

    payments = (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id)
        .order_by(Payment.created_at.asc())
        .all()
    )

    message_rows = (
        db.query(BookingMessage)
        .filter(BookingMessage.booking_id == booking.id)
        .order_by(BookingMessage.created_at.asc())
        .all()
    )
    sender_ids = {row.sender_user_id for row in message_rows}
    senders = (
        {u.id: u for u in db.query(User).filter(User.id.in_(sender_ids)).all()}
        if sender_ids
        else {}
    )
    messages = [
        BookingMessageOut(
            id=row.id,
            booking_id=row.booking_id,
            sender_user_id=row.sender_user_id,
            sender_name=(
                senders[row.sender_user_id].fullname
                if row.sender_user_id in senders
                else None
            ),
            body=row.body,
            created_at=row.created_at,
        )
        for row in message_rows
    ]

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )

    review_rows = (
        db.query(BookingReview)
        .filter(BookingReview.booking_id == booking.id)
        .order_by(BookingReview.created_at.asc())
        .all()
    )

    return AdminBookingDetailOut(
        **base.model_dump(),
        musician_phone=musician.user.phone if musician and musician.user else None,
        contractor_phone=(
            contractor.user.phone if contractor and contractor.user else None
        ),
        balance_due=float(due),
        amount_paid=float(paid),
        contract=ContractOut.model_validate(contract) if contract else None,
        payments=[PaymentOut.model_validate(p) for p in payments],
        messages=messages,
        complaint=serialize_complaint(complaint),
        reviews=[serialize_review(r) for r in review_rows],
    )


@router.get("/bookings/{booking_id}/contract-pdf")
def get_booking_admin_contract_pdf(
    booking_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from app.api.v1.endpoints.contracts import render_booking_contract_pdf_response

    booking = _load_booking_with_parties(db, booking_id)
    return render_booking_contract_pdf_response(db, booking, booking_id)


@router.post("/bookings/{booking_id}/cancel", response_model=AdminBookingOut)
def cancel_booking_admin(
    booking_id: str,
    payload: AdminBookingCancel,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    if booking.status in {BookingStatus.completed, BookingStatus.cancelled}:
        raise HTTPException(400, "Esta reserva ya está cerrada")

    booking.status = BookingStatus.cancelled
    booking.cancelled_by = "admin"
    if payload.reason:
        booking.rejection_reason = payload.reason
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


@router.post("/bookings/{booking_id}/disable-share", response_model=AdminBookingOut)
def disable_booking_share(
    booking_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    booking.share_enabled = False
    booking.share_enabled_at = None
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


# ── Payments ops ────────────────────────────────────────────────────────────


@router.get("/payments", response_model=list[AdminPaymentOut])
def list_payments(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    skip: int = 0,
    limit: int = Query(default=50, le=200),
):
    query = db.query(Payment).options(
        joinedload(Payment.booking)
        .joinedload(Booking.musician)
        .joinedload(MusicianProfile.user),
        joinedload(Payment.booking)
        .joinedload(Booking.contractor)
        .joinedload(ContractorProfile.user),
    )
    if status_filter:
        try:
            query = query.filter(Payment.status == PaymentStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(400, "Estado de pago inválido") from exc
    payments = query.order_by(Payment.created_at.desc()).offset(skip).limit(limit).all()
    return [_payment_admin_out(payment) for payment in payments]


def _load_booking_with_parties(db: Session, booking_id: str) -> Booking:
    booking = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.id == parse_uuid(booking_id, "booking_id"))
        .first()
    )
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    return booking


@router.get("/payments/pending-review", response_model=list[AdminPaymentReviewItem])
def list_pending_payment_reviews(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    bookings = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.status.in_(PAYMENT_REVIEW_STATUSES))
        .order_by(Booking.updated_at.asc())
        .all()
    )

    items: list[AdminPaymentReviewItem] = []
    for booking in bookings:
        kind = "advance" if booking.status == BookingStatus.payment_pending else "balance"
        payment_query = db.query(Payment).filter(Payment.booking_id == booking.id)
        if kind == "balance":
            payment_query = payment_query.filter(Payment.payment_type == "balance")
        payment = (
            payment_query.filter(Payment.status == PaymentStatus.initiated)
            .order_by(Payment.created_at.desc())
            .first()
        )
        if not payment:
            continue
        musician = booking.musician
        contractor = booking.contractor

        rejected_query = db.query(Payment).filter(
            Payment.booking_id == booking.id,
            Payment.status == PaymentStatus.rejected,
        )
        if kind == "balance":
            rejected_query = rejected_query.filter(Payment.payment_type == "balance")
        else:
            rejected_query = rejected_query.filter(Payment.payment_type != "balance")
        previous_rejections = rejected_query.count()

        items.append(
            AdminPaymentReviewItem(
                booking_id=booking.id,
                booking_status=booking.status.value,
                kind=kind,
                payment_id=payment.id,
                amount=payment.amount,
                currency=payment.currency or "PEN",
                evidence_urls=payment_evidence_list(payment),
                event_type=booking.event_type,
                event_date=booking.event_date,
                musician_name=(
                    musician.stage_name
                    if musician and musician.stage_name
                    else (musician.user.fullname if musician and musician.user else None)
                ),
                contractor_name=(
                    contractor.user.fullname if contractor and contractor.user else None
                ),
                submitted_at=payment.created_at,
                previous_rejections=previous_rejections,
            )
        )
    return items


@router.post("/bookings/{booking_id}/advance/validate", response_model=AdminBookingOut)
def validate_advance_payment_admin(
    booking_id: str,
    admin_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = _load_booking_with_parties(db, booking_id)
    payment_review.validate_advance_payment(db, booking, admin_user)

    notify_payment_validated(
        db,
        booking.contractor.user,
        booking.musician.user,
        str(booking.id),
    )
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


@router.post("/bookings/{booking_id}/advance/reject", response_model=AdminBookingOut)
def reject_advance_payment_admin(
    booking_id: str,
    payload: AdminPaymentReject,
    admin_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = _load_booking_with_parties(db, booking_id)
    reason = payload.reason.strip()
    payment_review.reject_advance_payment(db, booking, admin_user, reason)

    notify_payment_rejected(
        db,
        booking.contractor.user,
        booking.musician.user,
        str(booking.id),
        reason=reason,
    )
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


@router.post("/bookings/{booking_id}/balance/validate", response_model=AdminBookingOut)
def validate_balance_payment_admin(
    booking_id: str,
    admin_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = _load_booking_with_parties(db, booking_id)
    payment_review.validate_balance_payment(db, booking, admin_user)

    notify_balance_validated(
        db,
        booking.contractor.user,
        booking.musician.user,
        str(booking.id),
    )
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


@router.post("/bookings/{booking_id}/balance/reject", response_model=AdminBookingOut)
def reject_balance_payment_admin(
    booking_id: str,
    payload: AdminPaymentReject,
    admin_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    booking = _load_booking_with_parties(db, booking_id)
    reason = payload.reason.strip()
    payment_review.reject_balance_payment(db, booking, admin_user, reason)

    notify_balance_rejected(
        db,
        booking.contractor.user,
        booking.musician.user,
        str(booking.id),
        reason=reason,
    )
    db.commit()
    db.refresh(booking)
    return _booking_admin_out(booking)


@router.get("/payment-instructions", response_model=PlatformPaymentInstructionsOut)
def get_admin_payment_instructions(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from app.services.platform_payment import get_or_create_platform_payment_settings

    row = get_or_create_platform_payment_settings(db)
    if row.phone_number is None:
        row.phone_number = ""
    if row.phone_label is None:
        row.phone_label = "Yape / Plin"
    db.commit()
    return PlatformPaymentInstructionsOut.model_validate(row)


@router.put("/payment-instructions", response_model=PlatformPaymentInstructionsOut)
def update_admin_payment_instructions(
    payload: PlatformPaymentInstructionsUpdate,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from app.services.platform_payment import get_or_create_platform_payment_settings

    row = get_or_create_platform_payment_settings(db)
    row.phone_number = payload.phone_number.strip()
    row.phone_label = (payload.phone_label or "Yape / Plin").strip()
    row.account_name = (payload.account_name or "").strip() or None
    row.qr_image_url = payload.qr_image_url
    row.instructions = payload.instructions
    row.platform_fee_percent = payload.platform_fee_percent
    db.commit()
    db.refresh(row)
    return PlatformPaymentInstructionsOut.model_validate(row)


@router.get("/settlements")
def list_admin_settlements(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    state: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
):
    from decimal import Decimal

    from app.models.booking_complaint import BookingComplaint
    from app.schemas.settlement import AdminSettlementOut, MusicianPayoutInfoOut
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import (
        fee_portion_from_gross,
        released_total_for_booking,
        retained_total_for_booking,
        serialize_complaint,
        settlement_state_for_booking,
    )

    bookings = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.status == BookingStatus.completed)
        .order_by(Booking.updated_at.desc())
        .limit(limit * 3)
        .all()
    )
    complaints = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id.in_([b.id for b in bookings]) if bookings else False)
        .all()
        if bookings
        else []
    )
    complaint_by_id = {c.booking_id: c for c in complaints}

    items: list[AdminSettlementOut] = []
    for booking in bookings:
        retained_gross = retained_total_for_booking(db, booking.id)
        released_gross = released_total_for_booking(db, booking.id)
        retained = float(
            musician_portion_of_paid(booking, Decimal(str(retained_gross)))
        )
        released = float(
            musician_portion_of_paid(booking, Decimal(str(released_gross)))
        )
        complaint = complaint_by_id.get(booking.id)
        settlement_state = settlement_state_for_booking(
            booking, complaint, retained, released
        )
        if settlement_state in {"none", "in_progress"}:
            continue
        if state and settlement_state != state:
            continue
        musician = booking.musician
        contractor = booking.contractor

        payout_info = None
        if musician:
            payout_info = MusicianPayoutInfoOut(
                payout_method=musician.payout_method,
                payout_bank_name=musician.payout_bank_name,
                payout_account_number=musician.payout_account_number,
                payout_cci=musician.payout_cci,
                payout_phone=musician.payout_phone,
                payout_beneficiary_name=musician.payout_beneficiary_name,
                payout_beneficiary_document=musician.payout_beneficiary_document,
                payout_mp_email=musician.payout_mp_email,
            )

        latest_payment = (
            db.query(Payment)
            .filter(Payment.booking_id == booking.id)
            .order_by(Payment.created_at.desc())
            .first()
        )

        items.append(
            AdminSettlementOut(
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                price_agreed=(
                    float(booking.price_agreed) if booking.price_agreed is not None else None
                ),
                retained_total=retained,
                released_total=released,
                retained_gross=retained_gross,
                released_gross=released_gross,
                platform_fee_on_retained=fee_portion_from_gross(
                    booking, retained_gross
                ),
                musician_name=(
                    musician.stage_name
                    if musician and musician.stage_name
                    else (musician.user.fullname if musician and musician.user else None)
                ),
                musician_id=musician.id if musician else None,
                contractor_name=(
                    contractor.user.fullname if contractor and contractor.user else None
                ),
                booking_status=booking.status.value,
                settlement_state=settlement_state,
                complaint=serialize_complaint(complaint),
                musician_payout_info=payout_info,
                payout_reference=latest_payment.payout_reference if latest_payment else None,
                payout_evidence_url=latest_payment.payout_evidence_url if latest_payment else None,
                payout_notes=latest_payment.payout_notes if latest_payment else None,
            )
        )
        if len(items) >= limit:
            break

    return items


@router.get("/settlements/export")
def export_admin_settlements(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    state: str | None = Query(default=None),
):
    import csv
    import io
    from decimal import Decimal
    from fastapi.responses import Response
    from app.core.timezone import now_peru_naive
    from app.models.booking_complaint import BookingComplaint
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import (
        released_total_for_booking,
        retained_total_for_booking,
        settlement_state_for_booking,
    )

    bookings = (
        db.query(Booking)
        .options(
            joinedload(Booking.musician).joinedload(MusicianProfile.user),
            joinedload(Booking.contractor).joinedload(ContractorProfile.user),
        )
        .filter(Booking.status == BookingStatus.completed)
        .order_by(Booking.updated_at.desc())
        .all()
    )
    complaints = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id.in_([b.id for b in bookings]) if bookings else False)
        .all()
        if bookings
        else []
    )
    complaint_by_id = {c.booking_id: c for c in complaints}

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, dialect="excel")
    writer.writerow([
        "ID Reserva",
        "Fecha Evento",
        "Tipo Evento",
        "Ciudad",
        "Músico / Agrupación",
        "Monto a Desembolsar (S/)",
        "Método de Desembolso",
        "Banco",
        "Número de Cuenta",
        "CCI",
        "Celular Yape/Plin",
        "Titular Cuenta",
        "Documento Titular (DNI/RUC)",
        "Email Mercado Pago",
        "Contratante",
        "Estado Liquidación",
        "Referencia Operación",
        "Notas",
    ])

    for booking in bookings:
        retained_gross = retained_total_for_booking(db, booking.id)
        released_gross = released_total_for_booking(db, booking.id)
        retained = float(
            musician_portion_of_paid(booking, Decimal(str(retained_gross)))
        )
        released = float(
            musician_portion_of_paid(booking, Decimal(str(released_gross)))
        )
        complaint = complaint_by_id.get(booking.id)
        settlement_state = settlement_state_for_booking(
            booking, complaint, retained, released
        )
        if settlement_state in {"none", "in_progress"}:
            continue
        if state and settlement_state != state:
            continue

        musician = booking.musician
        contractor = booking.contractor
        musician_name = (
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else "")
        )
        contractor_name = (
            contractor.user.fullname if contractor and contractor.user else ""
        )
        payout_amount = retained if retained > 0 else released

        latest_payment = (
            db.query(Payment)
            .filter(Payment.booking_id == booking.id)
            .order_by(Payment.created_at.desc())
            .first()
        )

        payout_method_label = {
            "bank_transfer": "Transferencia Bancaria",
            "yape_plin": "Yape / Plin",
            "mercadopago": "Mercado Pago",
        }.get(musician.payout_method if musician else "", musician.payout_method if musician and musician.payout_method else "No registrado")

        writer.writerow([
            str(booking.id),
            str(booking.event_date),
            booking.event_type or "",
            booking.location_city or "",
            musician_name,
            f"{payout_amount:.2f}",
            payout_method_label,
            musician.payout_bank_name if musician else "",
            musician.payout_account_number if musician else "",
            musician.payout_cci if musician else "",
            musician.payout_phone if musician else "",
            musician.payout_beneficiary_name if musician else "",
            musician.payout_beneficiary_document if musician else "",
            musician.payout_mp_email if musician else "",
            contractor_name,
            settlement_state,
            latest_payment.payout_reference if latest_payment and latest_payment.payout_reference else "",
            latest_payment.payout_notes if latest_payment and latest_payment.payout_notes else "",
        ])

    csv_data = output.getvalue().encode("utf-8-sig")
    date_str = now_peru_naive().strftime("%Y%m%d_%H%M")
    headers = {
        "Content-Disposition": f'attachment; filename="liquidaciones_chivapp_{date_str}.csv"',
        "Content-Type": "text/csv; charset=utf-8",
    }
    return Response(content=csv_data, headers=headers, media_type="text/csv")


@router.post("/settlements/{booking_id}/settle")

def settle_admin_booking(
    booking_id: str,
    payload: AdminSettleBooking,
    current_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from app.models.booking_complaint import (
        REFUND_STATUS_AWAITING_TRANSFER,
        REFUND_STATUS_NONE,
        BookingComplaint,
        BookingComplaintStatus,
    )
    from app.schemas.settlement import AdminSettlementOut
    from app.services.booking_notifications import notify_settlement_completed
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import (
        fee_portion_from_gross,
        release_retained_payments,
        released_total_for_booking,
        retained_total_for_booking,
        serialize_complaint,
        settlement_state_for_booking,
    )

    body = payload
    booking = db.get(Booking, UUID(booking_id))
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    if booking.status != BookingStatus.completed:
        raise HTTPException(400, "Solo se liquidan reservas finalizadas")

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    retained_gross = retained_total_for_booking(db, booking.id)
    if retained_gross <= 0:
        raise HTTPException(400, "No hay fondos retenidos para liquidar")

    musician_pool = float(
        musician_portion_of_paid(booking, Decimal(str(retained_gross)))
    )
    total = round(body.musician_amount + body.contractor_refund, 2)
    if abs(total - round(musician_pool, 2)) > 0.01:
        raise HTTPException(
            400,
            "La suma de montos (S/ "
            f"{total:.2f}) debe igualar la deuda al músico "
            f"(S/ {musician_pool:.2f}). La comisión de plataforma no se reparte.",
        )

    if complaint:
        if complaint.status == BookingComplaintStatus.open:
            raise HTTPException(
                400,
                "El músico aún no responde la queja. Espera su aceptación o descargo.",
            )
        if complaint.status == BookingComplaintStatus.settled:
            raise HTTPException(400, "La queja ya fue liquidada")
        complaint.admin_musician_amount = body.musician_amount
        complaint.admin_contractor_refund = body.contractor_refund
        complaint.admin_notes = body.notes
        complaint.settled_by_admin_id = current_user.id
        complaint.settled_at = datetime.utcnow()
        complaint.status = BookingComplaintStatus.settled
        if body.contractor_refund > 0:
            from app.models.payment import Payment
            from app.services.mercadopago_service import issue_refund
            
            original_payment = db.query(Payment).filter(
                Payment.booking_id == booking.id,
                Payment.gateway_payment_id.isnot(None)
            ).first()
            
            if not original_payment:
                 raise HTTPException(400, "No se encontró el pago original en Mercado Pago para reembolsar automáticamente")
            
            try:
                issue_refund(original_payment.gateway_payment_id, float(body.contractor_refund))
                complaint.refund_status = REFUND_STATUS_COMPLETED
                complaint.refund_validated_at = datetime.utcnow()
                complaint.refund_rejection_reason = None
                complaint.refund_payment_id = original_payment.id
                complaint.refund_evidence_url = "Reembolso automático de Mercado Pago"
                complaint.refund_sent_at = datetime.utcnow()
            except Exception as e:
                raise HTTPException(500, f"Error al ejecutar reembolso en Mercado Pago: {str(e)}")
        else:
            complaint.refund_status = REFUND_STATUS_NONE
    elif body.contractor_refund > 0:
        raise HTTPException(
            400,
            "Sin queja no aplica reembolso al contratista. Desembolsa el total al músico.",
        )

    release_retained_payments(
        db,
        booking.id,
        payout_reference=body.payout_reference,
        payout_evidence_url=body.payout_evidence_url,
        payout_notes=body.notes,
    )

    musician = (
        db.query(MusicianProfile)
        .options(joinedload(MusicianProfile.user))
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    contractor = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if musician and musician.user and contractor and contractor.user:
        notify_settlement_completed(
            db,
            musician_user=musician.user,
            contractor_user=contractor.user,
            booking_id=str(booking.id),
            musician_amount=body.musician_amount,
            contractor_refund=body.contractor_refund,
        )

    db.commit()

    released_gross = released_total_for_booking(db, booking.id)
    released = float(
        musician_portion_of_paid(booking, Decimal(str(released_gross)))
    )
    complaint_out = serialize_complaint(complaint)

    payout_info = None
    if musician:
        from app.schemas.settlement import MusicianPayoutInfoOut
        payout_info = MusicianPayoutInfoOut(
            payout_method=musician.payout_method,
            payout_bank_name=musician.payout_bank_name,
            payout_account_number=musician.payout_account_number,
            payout_cci=musician.payout_cci,
            payout_phone=musician.payout_phone,
            payout_beneficiary_name=musician.payout_beneficiary_name,
            payout_beneficiary_document=musician.payout_beneficiary_document,
            payout_mp_email=musician.payout_mp_email,
        )

    return AdminSettlementOut(
        booking_id=booking.id,
        event_type=booking.event_type,
        event_date=booking.event_date,
        location_city=booking.location_city,
        price_agreed=(
            float(booking.price_agreed) if booking.price_agreed is not None else None
        ),
        retained_total=0,
        released_total=released,
        retained_gross=0,
        released_gross=released_gross,
        platform_fee_on_retained=0,
        musician_name=(
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else None)
        ),
        musician_id=musician.id if musician else None,
        contractor_name=(
            contractor.user.fullname if contractor and contractor.user else None
        ),
        booking_status=booking.status.value,
        settlement_state=settlement_state_for_booking(booking, complaint, 0, released),
        complaint=complaint_out,
        musician_payout_info=payout_info,
        payout_reference=body.payout_reference,
        payout_evidence_url=body.payout_evidence_url,
        payout_notes=body.notes,
    )


@router.post("/settlements/{booking_id}/release", response_model=AdminSettlementOut)
def release_admin_settlement(
    booking_id: str,
    payload: AdminReleaseSettlement = None,
    current_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from decimal import Decimal
    from uuid import UUID

    from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
    from app.schemas.settlement import AdminSettlementOut, MusicianPayoutInfoOut
    from app.services.booking_notifications import notify_settlement_completed
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import (
        release_retained_payments,
        released_total_for_booking,
        retained_total_for_booking,
        serialize_complaint,
        settlement_state_for_booking,
    )

    body = payload or AdminReleaseSettlement()
    booking = db.get(Booking, UUID(booking_id))
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    if booking.status != BookingStatus.completed:
        raise HTTPException(400, "Solo se liquidan reservas finalizadas")

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if complaint and complaint.status != BookingComplaintStatus.settled:
        raise HTTPException(
            400,
            "Esta reserva tiene un reclamo activo. Liquídala a través del flujo de reclamos.",
        )

    retained_gross = retained_total_for_booking(db, booking.id)
    if retained_gross <= 0:
        raise HTTPException(400, "No hay fondos retenidos para liquidar")

    musician_amount = float(
        musician_portion_of_paid(booking, Decimal(str(retained_gross)))
    )

    release_retained_payments(
        db,
        booking.id,
        payout_reference=body.payout_reference,
        payout_evidence_url=body.payout_evidence_url,
        payout_notes=body.payout_notes,
    )

    musician = (
        db.query(MusicianProfile)
        .options(joinedload(MusicianProfile.user))
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    contractor = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if musician and musician.user and contractor and contractor.user:
        notify_settlement_completed(
            db,
            musician_user=musician.user,
            contractor_user=contractor.user,
            booking_id=str(booking.id),
            musician_amount=musician_amount,
            contractor_refund=0.0,
        )

    db.commit()

    released_gross = released_total_for_booking(db, booking.id)
    released = float(
        musician_portion_of_paid(booking, Decimal(str(released_gross)))
    )

    payout_info = None
    if musician:
        payout_info = MusicianPayoutInfoOut(
            payout_method=musician.payout_method,
            payout_bank_name=musician.payout_bank_name,
            payout_account_number=musician.payout_account_number,
            payout_cci=musician.payout_cci,
            payout_phone=musician.payout_phone,
            payout_beneficiary_name=musician.payout_beneficiary_name,
            payout_beneficiary_document=musician.payout_beneficiary_document,
            payout_mp_email=musician.payout_mp_email,
        )

    return AdminSettlementOut(
        booking_id=booking.id,
        event_type=booking.event_type,
        event_date=booking.event_date,
        location_city=booking.location_city,
        price_agreed=(
            float(booking.price_agreed) if booking.price_agreed is not None else None
        ),
        retained_total=0,
        released_total=released,
        retained_gross=0,
        released_gross=released_gross,
        platform_fee_on_retained=0,
        musician_name=(
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else None)
        ),
        musician_id=musician.id if musician else None,
        contractor_name=(
            contractor.user.fullname if contractor and contractor.user else None
        ),
        booking_status=booking.status.value,
        settlement_state=settlement_state_for_booking(booking, complaint, 0, released),
        complaint=serialize_complaint(complaint),
        musician_payout_info=payout_info,
        payout_reference=body.payout_reference,
        payout_evidence_url=body.payout_evidence_url,
        payout_notes=body.payout_notes,
    )



@router.post("/settlements/{booking_id}/refund-transfer")
def send_admin_refund_transfer(
    booking_id: str,
    payload: AdminRefundTransfer,
    current_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from app.models.booking_complaint import (
        REFUND_STATUS_AWAITING_TRANSFER,
        REFUND_STATUS_AWAITING_VALIDATION,
        REFUND_STATUS_REJECTED,
        BookingComplaint,
        BookingComplaintStatus,
    )
    from app.schemas.settlement import AdminSettlementOut
    from app.services.booking_notifications import notify_refund_transfer_sent
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import (
        complaint_refund_amount,
        fee_portion_from_gross,
        released_total_for_booking,
        retained_total_for_booking,
        serialize_complaint,
        settlement_state_for_booking,
    )

    body = payload
    booking = db.get(Booking, UUID(booking_id))
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not complaint or complaint.status != BookingComplaintStatus.settled:
        raise HTTPException(400, "La queja debe estar liquidada primero")

    refund_amount = complaint_refund_amount(complaint)
    if refund_amount <= 0:
        raise HTTPException(400, "Esta liquidación no incluye devolución al contratista")

    rs = complaint.refund_status or REFUND_STATUS_AWAITING_TRANSFER
    if rs not in {
        REFUND_STATUS_AWAITING_TRANSFER,
        REFUND_STATUS_REJECTED,
        "none",
    }:
        raise HTTPException(
            400,
            "La devolución ya fue enviada o completada. Espera la validación del contratista.",
        )

    evidence = (body.evidence_url or "").strip()
    if not evidence:
        raise HTTPException(400, "Debes adjuntar el comprobante de la transferencia")

    payment = None
    if complaint.refund_payment_id:
        payment = db.get(Payment, complaint.refund_payment_id)
    if payment is None:
        payment = Payment(
            booking_id=booking.id,
            amount=refund_amount,
            currency="PEN",
            payment_type="refund",
            evidence_url=evidence,
            status=PaymentStatus.initiated,
        )
        db.add(payment)
        db.flush()
        complaint.refund_payment_id = payment.id
    else:
        payment.amount = refund_amount
        payment.payment_type = "refund"
        payment.evidence_url = evidence
        payment.status = PaymentStatus.initiated

    complaint.refund_evidence_url = evidence
    complaint.refund_sent_at = datetime.utcnow()
    complaint.refund_sent_by_admin_id = current_user.id
    complaint.refund_status = REFUND_STATUS_AWAITING_VALIDATION
    complaint.refund_rejection_reason = None
    if body.notes:
        note = (complaint.admin_notes or "").strip()
        extra = body.notes.strip()
        complaint.admin_notes = f"{note}\n{extra}".strip() if note else extra

    contractor = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if contractor and contractor.user:
        notify_refund_transfer_sent(
            db,
            contractor_user=contractor.user,
            booking_id=str(booking.id),
            amount=refund_amount,
        )

    db.commit()
    db.refresh(complaint)

    retained_gross = retained_total_for_booking(db, booking.id)
    released_gross = released_total_for_booking(db, booking.id)
    retained = float(
        musician_portion_of_paid(booking, Decimal(str(retained_gross)))
    )
    released = float(
        musician_portion_of_paid(booking, Decimal(str(released_gross)))
    )
    musician = (
        db.query(MusicianProfile)
        .options(joinedload(MusicianProfile.user))
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    return AdminSettlementOut(
        booking_id=booking.id,
        event_type=booking.event_type,
        event_date=booking.event_date,
        location_city=booking.location_city,
        price_agreed=(
            float(booking.price_agreed) if booking.price_agreed is not None else None
        ),
        retained_total=retained,
        released_total=released,
        retained_gross=retained_gross,
        released_gross=released_gross,
        platform_fee_on_retained=fee_portion_from_gross(booking, retained_gross),
        musician_name=(
            musician.stage_name
            if musician and musician.stage_name
            else (musician.user.fullname if musician and musician.user else None)
        ),
        contractor_name=(
            contractor.user.fullname if contractor and contractor.user else None
        ),
        booking_status=booking.status.value,
        settlement_state=settlement_state_for_booking(
            booking, complaint, retained, released
        ),
        complaint=serialize_complaint(complaint),
    )


# ── Activity ────────────────────────────────────────────────────────────────


@router.get("/activity", response_model=list[AdminActivityItem])
def get_admin_activity(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    limit: int = Query(default=30, le=100),
):
    activity: list[AdminActivityItem] = []

    recent_users = db.query(User).order_by(User.created_at.desc()).limit(limit).all()
    for user in recent_users:
        activity.append(
            AdminActivityItem(
                id=str(user.id),
                type="user_registered",
                title=f"Nuevo usuario: {user.fullname}",
                subtitle=f"{user.role.value} · {user.email}",
                href="/admin/users",
                created_at=user.created_at,
            )
        )

    pending_musicians = (
        db.query(MusicianProfile)
        .join(User)
        .filter(MusicianProfile.status == ProfileStatus.pending_review)
        .order_by(MusicianProfile.submitted_at.desc())
        .limit(limit)
        .all()
    )
    for profile in pending_musicians:
        if profile.submitted_at:
            activity.append(
                AdminActivityItem(
                    id=str(profile.id),
                    type="musician_submitted",
                    title=f"Perfil músico enviado: {profile.stage_name}",
                    subtitle=profile.user.email,
                    href="/admin/musicians?status=pending_review",
                    created_at=profile.submitted_at,
                )
            )

    pending_contractors = (
        db.query(ContractorProfile)
        .join(User)
        .filter(ContractorProfile.status == ProfileStatus.pending_review)
        .order_by(ContractorProfile.submitted_at.desc())
        .limit(limit)
        .all()
    )
    for profile in pending_contractors:
        if profile.submitted_at:
            activity.append(
                AdminActivityItem(
                    id=str(profile.id),
                    type="contractor_submitted",
                    title=f"Perfil contratista enviado: {profile.user.fullname}",
                    subtitle=profile.user.email,
                    href="/admin/contractors?status=pending_review",
                    created_at=profile.submitted_at,
                )
            )

    recent_bookings = (
        db.query(Booking).order_by(Booking.updated_at.desc()).limit(limit).all()
    )
    for booking in recent_bookings:
        activity.append(
            AdminActivityItem(
                id=str(booking.id),
                type="booking_updated",
                title=f"Reserva {booking.event_type}",
                subtitle=f"{booking.status.value} · {booking.location_city or booking.location_address}",
                href="/admin/bookings",
                created_at=booking.updated_at or booking.created_at,
            )
        )

    recent_payments = (
        db.query(Payment).order_by(Payment.created_at.desc()).limit(limit).all()
    )
    for payment in recent_payments:
        activity.append(
            AdminActivityItem(
                id=str(payment.id),
                type="payment_event",
                title=f"Pago {payment.payment_type or '—'} · S/ {payment.amount}",
                subtitle=str(payment.status.value if hasattr(payment.status, 'value') else payment.status),
                href="/admin/payments",
                created_at=payment.created_at,
            )
        )

    activity.sort(key=lambda item: item.created_at, reverse=True)
    return activity[:limit]


def _support_ticket_admin_out(ticket: SupportTicket) -> AdminSupportTicketOut:
    return AdminSupportTicketOut(
        id=ticket.id,
        user_id=ticket.user_id,
        submitter_name=ticket.user.fullname if ticket.user else ticket.guest_name,
        submitter_email=ticket.user.email if ticket.user else ticket.guest_email,
        message=ticket.message,
        status=ticket.status.value,
        admin_response=ticket.admin_response,
        responded_at=ticket.responded_at,
        created_at=ticket.created_at,
        submitted_ip=ticket.submitted_ip,
        submitted_user_agent=ticket.submitted_user_agent,
        submitted_path=ticket.submitted_path,
    )


@router.get("/support/tickets", response_model=list[AdminSupportTicketOut])
def list_support_tickets(
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    skip: int = 0,
    limit: int = Query(default=50, le=200),
):
    query = db.query(SupportTicket).options(joinedload(SupportTicket.user))
    if status_filter:
        try:
            query = query.filter(SupportTicket.status == SupportTicketStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(400, "Estado inválido") from exc
    tickets = (
        query.order_by(SupportTicket.created_at.desc()).offset(skip).limit(limit).all()
    )
    return [_support_ticket_admin_out(ticket) for ticket in tickets]


@router.get("/support/tickets/{ticket_id}", response_model=AdminSupportTicketOut)
def get_support_ticket(
    ticket_id: str,
    _: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    ticket = db.get(SupportTicket, parse_uuid(ticket_id, "ticket_id"))
    if not ticket:
        raise HTTPException(404, "Ticket no encontrado")
    return _support_ticket_admin_out(ticket)


@router.post("/support/tickets/{ticket_id}/respond", response_model=AdminSupportTicketOut)
def respond_support_ticket(
    ticket_id: str,
    payload: SupportTicketRespond,
    current_admin: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    ticket = db.get(SupportTicket, parse_uuid(ticket_id, "ticket_id"))
    if not ticket:
        raise HTTPException(404, "Ticket no encontrado")

    try:
        new_status = SupportTicketStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(400, "Estado inválido") from exc

    ticket.admin_response = payload.response
    ticket.status = new_status
    ticket.responded_by_admin_id = current_admin.id
    ticket.responded_at = datetime.utcnow()
    db.commit()
    db.refresh(ticket)
    return _support_ticket_admin_out(ticket)
