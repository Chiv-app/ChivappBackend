from app.services.email.auth_emails import send_welcome_email
from app.core.hashing import hash_password
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_musician_owner,
    booking_parties_load_options,
    get_booking_member_invite,
    resolve_member_invite_status,
    serialize_booking_out,
)
from app.models.booking import Booking, BookingStatus
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    BookingMemberPayout,
    BookingMemberPayoutStatus,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.user import User, UserRole
from app.schemas.booking import BookingOut
from app.schemas.ensemble import (
    BookingMemberInviteCreate,
    BookingMemberInviteOut,
    BookingMemberInvitePreviewOut,
    BookingMemberPayoutOut,
    BookingMemberPayoutUpdate,
    BookingMemberRespondRequest,
    EnsembleMemberCreate,
    EnsembleMemberOut,
    EnsembleMemberUpdate,
    MemberPayoutHistoryItem,
    MemberPayoutHistoryOut,
    MemberSettlementBookingOut,
    MemberSettlementLineOut,
    MusicianReportsOut,
    MyMemberIncomeItem,
    MyMemberIncomeSummary,
)
from app.services.booking_notifications import notify_user
from app.services.email.auth_emails import (
    send_booking_member_invite_email,
    send_ensemble_invite_email,
)
from app.services.ensemble_members import (
    PASSWORD_SETUP_TTL_DAYS,
    create_or_link_member_user,
    get_leader_musician_or_403,
    get_member_for_leader,
    issue_password_setup_token,
    new_token,
    password_setup_url,
    serialize_booking_invite,
    serialize_member,
    serialize_payout,
)

router = APIRouter(tags=["Ensemble members"])

# Tras adelanto validado (reserva confirmada). Sin completed: ya no se convocan.
INVITE_STATUSES = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.balance_pending,
    BookingStatus.balance_review,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
}

# Fase de evento / cierre: se puede pagar a integrantes tras reseña al contratista.
PAYOUT_ELIGIBLE_STATUSES = {
    BookingStatus.in_progress,
    BookingStatus.payment_released,
    BookingStatus.completed,
}


def _generate_ics_attachment(booking, leader_name: str) -> dict | None:
    try:
        import datetime as dt
        from zoneinfo import ZoneInfo
        import base64

        lima_tz = ZoneInfo('America/Lima')
        start_dt = dt.datetime.combine(booking.event_date, booking.start_time)
        if booking.end_time:
            end_dt = dt.datetime.combine(booking.event_date, booking.end_time)
            if end_dt < start_dt:
                end_dt = end_dt + dt.timedelta(days=1)
        else:
            end_dt = start_dt + dt.timedelta(hours=1)
            
        start_utc = start_dt.replace(tzinfo=lima_tz).astimezone(dt.timezone.utc)
        end_utc = end_dt.replace(tzinfo=lima_tz).astimezone(dt.timezone.utc)
        
        fmt = "%Y%m%dT%H%M%SZ"
        
        uid = f"{booking.id}@chiv.app"
        dtstamp = dt.datetime.now(dt.timezone.utc).strftime(fmt)
        
        location = f"{booking.location_address}, {booking.location_city or ''}"
        
        ics = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Chivapp//NONSGML v1.0//EN\r\nCALSCALE:GREGORIAN\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{dtstamp}\r\nDTSTART:{start_utc.strftime(fmt)}\r\nDTEND:{end_utc.strftime(fmt)}\r\nSUMMARY:Chivapp: {booking.event_type} - {leader_name}\r\nLOCATION:{location}\r\nDESCRIPTION:Reserva confirmada en Chivapp.\r\nSTATUS:CONFIRMED\r\nBEGIN:VALARM\r\nTRIGGER:-PT120M\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR"
        
        return {
            "name": "invitacion.ics",
            "content": base64.b64encode(ics.encode("utf-8")).decode("ascii")
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error generando ICS: {e}")
        return None

def _has_musician_contractor_review(db: Session, booking: Booking) -> bool:
    return (
        db.query(ContractorRecommendation.id)
        .filter(ContractorRecommendation.booking_id == booking.id)
        .first()
        is not None
    )


def _assert_can_manage_member_payouts(
    db: Session,
    booking: Booking,
    current_user: User,
) -> None:
    assert_booking_musician_owner(db, booking, current_user)
    if booking.status not in PAYOUT_ELIGIBLE_STATUSES:
        raise HTTPException(
            400,
            "Solo puedes pagar a tus integrantes después de finalizar el show",
        )
    if not _has_musician_contractor_review(db, booking):
        raise HTTPException(
            400,
            "Primero deja tu reseña al contratista para habilitar los pagos a integrantes",
        )


@router.get("/musician/members", response_model=list[EnsembleMemberOut])
def list_members(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    query = db.query(EnsembleMember).filter(
        EnsembleMember.leader_user_id == current_user.id
    )
    if status:
        try:
            status_enum = EnsembleMemberStatus(status)
        except ValueError as exc:
            raise HTTPException(400, "Estado inválido") from exc
        query = query.filter(EnsembleMember.status == status_enum)
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            (EnsembleMember.fullname.ilike(term)) | (EnsembleMember.email.ilike(term))
        )
    members = query.order_by(EnsembleMember.fullname.asc()).all()
    # Siempre incluir invite_url si aún no creó contraseña (serialize lo filtra).
    return [serialize_member(m, include_invite_url=True) for m in members]


@router.post("/musician/members", response_model=EnsembleMemberOut, status_code=201)
def create_member(
    payload: EnsembleMemberCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    email = str(payload.email).lower().strip()

    existing = (
        db.query(EnsembleMember)
        .filter(
            EnsembleMember.leader_user_id == current_user.id,
            EnsembleMember.email == email,
        )
        .first()
    )
    if existing and existing.status != EnsembleMemberStatus.inactive:
        raise HTTPException(400, "Ese integrante ya está en tu agrupación")

    member_user = create_or_link_member_user(
        db,
        leader=current_user,
        email=email,
        fullname=payload.fullname,
        phone=payload.phone,
        specialties=payload.specialties,
    )

    has_password = bool(member_user.password_hash)
    if existing:
        member = existing
        member.fullname = payload.fullname
        member.phone = payload.phone
        member.specialties = payload.specialties
        member.notes = payload.notes
        member.member_user_id = member_user.id
        member.status = (
            EnsembleMemberStatus.active if has_password else EnsembleMemberStatus.invited
        )
    else:
        member = EnsembleMember(
            leader_user_id=current_user.id,
            member_user_id=member_user.id,
            email=email,
            fullname=payload.fullname,
            phone=payload.phone,
            specialties=payload.specialties,
            notes=payload.notes,
            status=(
                EnsembleMemberStatus.active
                if has_password
                else EnsembleMemberStatus.invited
            ),
            invited_at=datetime.utcnow(),
            joined_at=datetime.utcnow() if has_password else None,
        )
        db.add(member)

    db.flush()

    invite_url = None
    if not has_password:
        token = issue_password_setup_token(member)
        invite_url = password_setup_url(token)

    if member_user.id != current_user.id:
        notify_user(
            db,
            user=member_user,
            type="ensemble_invite",
            title="Te invitaron a una agrupación",
            message=(
                f"{current_user.fullname} te agregó como integrante. "
                + (
                    "Crea tu contraseña con el enlace de invitación para continuar."
                    if not has_password
                    else "Ya puedes ver las convocatorias de la agrupación."
                )
            ),
            meta={
                "ensemble_member_id": str(member.id),
                "invite_url": invite_url,
            },
        )
        if invite_url:
            send_ensemble_invite_email(
                db,
                member_email=email,
                member_name=member.fullname,
                leader_name=current_user.fullname,
                invite_url=invite_url,
                specialties=list(member.specialties or []),
                user_id=member_user.id,
                expires_days=PASSWORD_SETUP_TTL_DAYS,
            )

    db.commit()
    db.refresh(member)
    return serialize_member(member, include_invite_url=True)


@router.get("/musician/members/{member_id}", response_model=EnsembleMemberOut)
def get_member(
    member_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    member = get_member_for_leader(
        db, leader_id=current_user.id, member_id=member_id
    )
    return serialize_member(member, include_invite_url=True)


@router.get(
    "/musician/members/{member_id}/payouts",
    response_model=MemberPayoutHistoryOut,
)
def list_member_payout_history(
    member_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    status: str | None = Query(default=None),
):
    """Historial de pagos/repartos de un integrante (vista del líder)."""
    get_leader_musician_or_403(db, current_user)
    member = get_member_for_leader(
        db, leader_id=current_user.id, member_id=member_id
    )
    payouts = (
        db.query(BookingMemberPayout)
        .options(joinedload(BookingMemberPayout.booking))
        .filter(BookingMemberPayout.ensemble_member_id == member.id)
        .order_by(BookingMemberPayout.created_at.desc())
        .all()
    )

    items: list[MemberPayoutHistoryItem] = []
    total_assigned = Decimal("0")
    total_pending = Decimal("0")
    total_paid = Decimal("0")
    booking_ids: set[UUID] = set()

    for payout in payouts:
        payout_status = (
            payout.status.value
            if hasattr(payout.status, "value")
            else str(payout.status)
        )
        if status and payout_status != status:
            continue
        booking = payout.booking
        if not booking:
            continue
        amount = Decimal(str(payout.amount or 0))
        total_assigned += amount
        if payout_status == BookingMemberPayoutStatus.paid.value:
            total_paid += amount
        else:
            total_pending += amount
        booking_ids.add(booking.id)
        items.append(
            MemberPayoutHistoryItem(
                payout_id=payout.id,
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                booking_status=booking.status.value,
                amount=amount,
                currency=payout.currency or "PEN",
                status=payout_status,
                note=payout.note,
                set_by_leader_at=payout.set_by_leader_at,
                paid_at=payout.paid_at,
            )
        )

    return MemberPayoutHistoryOut(
        member_id=member.id,
        member_fullname=member.fullname,
        member_email=member.email,
        total_assigned=total_assigned,
        total_pending=total_pending,
        total_paid=total_paid,
        shows_count=len(booking_ids),
        items=items,
    )


@router.get("/musician/reports", response_model=MusicianReportsOut)
def get_musician_reports(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    group_by: str = Query(default="month"),
    status: str | None = Query(default=None),
):
    """Reportes agregados del músico líder (filtros + series)."""
    from app.services.musician_reports import build_musician_reports

    return build_musician_reports(
        db,
        current_user,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        status=status,
    )


@router.patch("/musician/members/{member_id}", response_model=EnsembleMemberOut)
def update_member(
    member_id: UUID,
    payload: EnsembleMemberUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    member = get_member_for_leader(
        db, leader_id=current_user.id, member_id=member_id
    )
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        try:
            new_status = EnsembleMemberStatus(data["status"])
        except ValueError as exc:
            raise HTTPException(400, "Estado inválido") from exc
        member.status = new_status
        del data["status"]
    for field, value in data.items():
        setattr(member, field, value)
    member.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(member)
    return serialize_member(member, include_invite_url=True)


@router.post(
    "/musician/members/{member_id}/resend-invite",
    response_model=EnsembleMemberOut,
)
def resend_invite(
    member_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    member = get_member_for_leader(
        db, leader_id=current_user.id, member_id=member_id
    )
    if not member.member_user_id:
        raise HTTPException(400, "El integrante no tiene cuenta vinculada")
    user = db.get(User, member.member_user_id)
    if user and user.password_hash:
        raise HTTPException(400, "Este integrante ya creó su contraseña")

    token = issue_password_setup_token(member)
    member.status = EnsembleMemberStatus.invited
    member.invited_at = datetime.utcnow()
    invite_url = password_setup_url(token)

    if user:
        notify_user(
            db,
            user=user,
            type="ensemble_invite",
            title="Reenvío de invitación",
            message=f"{current_user.fullname} te reenvió el enlace para crear tu contraseña.",
            meta={"ensemble_member_id": str(member.id), "invite_url": invite_url},
        )
        send_ensemble_invite_email(
            db,
            member_email=member.email,
            member_name=member.fullname,
            leader_name=current_user.fullname,
            invite_url=invite_url,
            specialties=list(member.specialties or []),
            user_id=user.id if user else None,
            expires_days=PASSWORD_SETUP_TTL_DAYS,
        )

    db.commit()
    db.refresh(member)
    return serialize_member(member, include_invite_url=True)


@router.delete("/musician/members/{member_id}", response_model=EnsembleMemberOut)
def deactivate_member(
    member_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    get_leader_musician_or_403(db, current_user)
    member = get_member_for_leader(
        db, leader_id=current_user.id, member_id=member_id
    )
    member.status = EnsembleMemberStatus.inactive
    member.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(member)
    return serialize_member(member, include_invite_url=False)


# --- Booking convocatorias & payouts ---


@router.get(
    "/bookings/{booking_id}/member-invites",
    response_model=list[BookingMemberInviteOut],
)
def list_booking_invites(
    booking_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    assert_booking_musician_owner(db, booking, current_user)
    invites = (
        db.query(BookingMemberInvite)
        .filter(BookingMemberInvite.booking_id == booking_id)
        .order_by(BookingMemberInvite.invited_at.desc())
        .all()
    )
    return [serialize_booking_invite(i) for i in invites]


@router.post(
    "/bookings/{booking_id}/member-invites",
    response_model=list[BookingMemberInviteOut],
    status_code=201,
)
def create_booking_invites(
    booking_id: UUID,
    payload: BookingMemberInviteCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    assert_booking_musician_owner(db, booking, current_user)
    if booking.status not in INVITE_STATUSES:
        raise HTTPException(
            400,
            "Solo puedes elegir integrantes cuando el adelanto está confirmado "
            "y el show aún no se cerró",
        )

    results: list[BookingMemberInvite] = []
    from app.services.calendar_service import add_attendee_to_booking_event

    for member_id in payload.ensemble_member_ids:
        member = get_member_for_leader(
            db, leader_id=current_user.id, member_id=member_id
        )
        if member.status == EnsembleMemberStatus.inactive:
            continue

        invite = (
            db.query(BookingMemberInvite)
            .filter(
                BookingMemberInvite.booking_id == booking_id,
                BookingMemberInvite.ensemble_member_id == member.id,
            )
            .first()
        )
        if invite:
            if invite.status != BookingMemberInviteStatus.pending:
                invite.status = BookingMemberInviteStatus.pending
                invite.responded_at = None
            invite.response_token = new_token()
            invite.invited_at = datetime.utcnow()
        else:
            invite = BookingMemberInvite(
                booking_id=booking_id,
                ensemble_member_id=member.id,
                status=BookingMemberInviteStatus.pending,
                response_token=new_token(),
                invited_at=datetime.utcnow(),
            )
            db.add(invite)
        db.flush()
        results.append(invite)

        # AGREGAR A GOOGLE CALENDAR
        if booking.calendar_event_id:
            add_attendee_to_booking_event(booking.calendar_event_id, member.email)

        invite_data = serialize_booking_invite(invite)
        respond_url = invite_data.get("respond_url")

        # In-app notification ONLY if the user is registered
        if member.member_user_id:
            member_user = db.get(User, member.member_user_id)
            if member_user:
                notify_user(
                    db,
                    user=member_user,
                    type="booking_member_invite",
                    title="Nueva convocatoria a un evento",
                    message=(
                        f"{current_user.fullname} te convocó a "
                        f"{booking.event_type} el {booking.event_date}."
                    ),
                    booking_id=str(booking.id),
                    meta={
                        "respond_url": respond_url,
                        "ensemble_member_id": str(member.id),
                    },
                )

        # Email notification ALWAYS to the member's email (if they have one)
        if respond_url and member.email:
            event_time = booking.start_time or "Por confirmar"
            if booking.end_time:
                event_time = f"{booking.start_time or '?'} - {booking.end_time}"
            location_parts = [
                part for part in (booking.location_address, booking.location_city) if part
            ]
            
            ics_file = _generate_ics_attachment(booking, current_user.fullname)
            extra_atts = [ics_file] if ics_file else None

            send_booking_member_invite_email(
                db,
                member_email=member.email,
                member_name=member.fullname,
                leader_name=current_user.fullname,
                event_type=booking.event_type,
                event_date=str(booking.event_date),
                event_time=str(event_time),
                event_location=", ".join(location_parts) or "Por confirmar",
                respond_url=respond_url,
                user_id=member.member_user_id,
                booking_id=str(booking.id),
                extra_attachments=extra_atts,
            )

    db.commit()
    for invite in results:
        db.refresh(invite)
    return [serialize_booking_invite(i) for i in results]


@router.get(
    "/invites/member/{token}",
    response_model=BookingMemberInvitePreviewOut,
)
def preview_booking_invite(token: str, db: Session = Depends(deps.get_db)):
    invite = (
        db.query(BookingMemberInvite)
        .filter(BookingMemberInvite.response_token == token)
        .first()
    )
    if not invite:
        raise HTTPException(404, "Invitación no encontrada")
    booking = invite.booking
    member = invite.ensemble_member
    leader = db.get(User, member.leader_user_id) if member else None
    member_user = db.get(User, member.member_user_id) if member and member.member_user_id else None
    return {
        "event_type": booking.event_type,
        "event_date": booking.event_date,
        "start_time": booking.start_time,
        "location_city": booking.location_city,
        "location_address": booking.location_address,
        "leader_name": leader.fullname if leader else "Tu agrupación",
        "member_fullname": member.fullname if member else "",
        "status": invite.status.value,
        "can_respond": invite.status == BookingMemberInviteStatus.pending
        and booking.status != BookingStatus.cancelled,
        "needs_password": bool(member_user and not member_user.password_hash)
    }


@router.post(
    "/invites/member/{token}/respond",
    response_model=BookingMemberInvitePreviewOut,
)
def respond_booking_invite(
    token: str,
    payload: BookingMemberRespondRequest,
    db: Session = Depends(deps.get_db),
):
    invite = (
        db.query(BookingMemberInvite)
        .filter(BookingMemberInvite.response_token == token)
        .first()
    )
    if not invite:
        raise HTTPException(404, "Invitación no encontrada")

    member = invite.ensemble_member
    member_user = db.get(User, member.member_user_id) if member and member.member_user_id else None
    
    if payload.action == "accept" and member_user and not member_user.password_hash:
        if not payload.password:
            raise HTTPException(400, "Debes crear una contraseña para aceptar la convocatoria.")
        member_user.password_hash = hash_password(payload.password)
        member_user.email_verified_at = datetime.utcnow()
        db.add(member_user)
        # Update ensemble member status
        member.status = EnsembleMemberStatus.active
        db.add(member)
        # Also trigger welcome email since they just completed setup
        send_welcome_email(db, member_user)

    _apply_booking_invite_response(db, invite, payload.action)
    db.commit()
    return preview_booking_invite(token, db)


@router.post(
    "/bookings/{booking_id}/member-invites/{member_id}/resend",
    response_model=BookingMemberInviteOut,
)
def resend_booking_invite_email(
    booking_id: UUID,
    member_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Re-sends the booking invite email for a pending invite."""
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    assert_booking_musician_owner(db, booking, current_user)
    
    member = get_member_for_leader(db, leader_id=current_user.id, member_id=member_id)
    
    invite = (
        db.query(BookingMemberInvite)
        .filter(
            BookingMemberInvite.booking_id == booking_id,
            BookingMemberInvite.ensemble_member_id == member.id,
        )
        .first()
    )
    if not invite:
        raise HTTPException(404, "No existe una convocatoria para este integrante en esta reserva")
    if invite.status != BookingMemberInviteStatus.pending:
        raise HTTPException(400, f"No se puede reenviar porque la convocatoria ya fue {invite.status.value}")
        
    # Generate new token
    invite.response_token = new_token()
    invite.invited_at = datetime.utcnow()
    db.commit()
    db.refresh(invite)
    
    # Send email ALWAYS
    invite_data = serialize_booking_invite(invite)
    respond_url = invite_data.get("respond_url")
    if respond_url and member.email:
        event_time = booking.start_time or "Por confirmar"
        if booking.end_time:
            event_time = f"{booking.start_time or '?'} - {booking.end_time}"
        location_parts = [
            part for part in (booking.location_address, booking.location_city) if part
        ]
        
        ics_file = _generate_ics_attachment(booking, current_user.fullname)
        extra_atts = [ics_file] if ics_file else None
        
        from app.services.email.auth_emails import send_booking_member_invite_email
        send_booking_member_invite_email(
            db,
            member_email=member.email,
            member_name=member.fullname,
            leader_name=current_user.fullname,
            event_type=booking.event_type,
            event_date=str(booking.event_date),
            event_time=str(event_time),
            event_location=", ".join(location_parts) or "Por confirmar",
            respond_url=respond_url,
            user_id=member.member_user_id,
            booking_id=str(booking.id),
            extra_attachments=extra_atts,
        )
            
    return serialize_booking_invite(invite)


@router.post(
    "/bookings/{booking_id}/member-invites/me/respond",
    response_model=BookingOut,
)
def respond_my_booking_invite(
    booking_id: UUID,
    payload: BookingMemberRespondRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Authenticated RSVP for the logged-in ensemble member."""
    booking = (
        db.query(Booking)
        .options(*booking_parties_load_options())
        .filter(Booking.id == booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")

    invite = get_booking_member_invite(
        db,
        booking,
        current_user,
        statuses=[
            BookingMemberInviteStatus.pending,
            BookingMemberInviteStatus.accepted,
            BookingMemberInviteStatus.declined,
        ],
    )
    if not invite:
        raise HTTPException(403, "No tienes una convocatoria para esta reserva")

    _apply_booking_invite_response(db, invite, payload.action)
    db.commit()
    db.refresh(booking)

    return serialize_booking_out(
        booking,
        viewer_role="member",
        member_invite_status=resolve_member_invite_status(
            db, booking, current_user
        ),
    )


def _apply_booking_invite_response(
    db: Session,
    invite: BookingMemberInvite,
    action: str,
) -> None:
    booking = invite.booking
    if booking.status == BookingStatus.cancelled:
        raise HTTPException(400, "Esta reserva fue cancelada")
    if invite.status != BookingMemberInviteStatus.pending:
        raise HTTPException(400, "Ya respondiste esta convocatoria")

    invite.status = (
        BookingMemberInviteStatus.accepted
        if action == "accept"
        else BookingMemberInviteStatus.declined
    )
    invite.responded_at = datetime.utcnow()

    member = invite.ensemble_member
    
    # REMOVE FROM CALENDAR IF DECLINED
    if action == "decline" and booking.calendar_event_id and member:
        from app.services.calendar_service import remove_attendee_from_booking_event
        remove_attendee_from_booking_event(booking.calendar_event_id, member.email)

    leader = db.get(User, member.leader_user_id) if member else None
    if leader:
        action_label = "aceptó" if action == "accept" else "rechazó"
        notify_user(
            db,
            user=leader,
            type="booking_member_response",
            title=f"Integrante {action_label} la convocatoria",
            message=f"{member.fullname} {action_label} ir a {booking.event_type}.",
            booking_id=str(booking.id),
            meta={
                "ensemble_member_id": str(member.id),
                "status": invite.status.value,
            },
        )
        
        if action == "accept":
            from app.services.email.auth_emails import (
                send_booking_member_accepted_to_member_email,
                send_booking_member_accepted_to_leader_email
            )
            event_time = booking.start_time or "Por confirmar"
            if booking.end_time:
                event_time = f"{booking.start_time or '?'} – {booking.end_time}"
            location_parts = [
                part for part in (booking.location_address, booking.location_city) if part
            ]
            event_location = ", ".join(location_parts) or "Por confirmar"
            
            # Email to member
            member_user = db.get(User, member.member_user_id) if member.member_user_id else None
            if member_user and member.email:
                send_booking_member_accepted_to_member_email(
                    db,
                    member_email=member.email,
                    member_name=member.fullname,
                    leader_name=leader.fullname,
                    event_type=booking.event_type,
                    event_date=str(booking.event_date),
                    event_time=str(event_time),
                    event_location=event_location,
                    user_id=member_user.id,
                    booking_id=str(booking.id),
                )
            
            # Email to leader
            if leader.email:
                send_booking_member_accepted_to_leader_email(
                    db,
                    leader_email=leader.email,
                    leader_name=leader.fullname,
                    member_name=member.fullname,
                    event_type=booking.event_type,
                    event_date=str(booking.event_date),
                    event_time=str(event_time),
                    user_id=leader.id,
                    booking_id=str(booking.id),
                )


@router.get(
    "/bookings/{booking_id}/member-payouts",
    response_model=list[BookingMemberPayoutOut],
)
def list_member_payouts(
    booking_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    assert_booking_musician_owner(db, booking, current_user)
    payouts = (
        db.query(BookingMemberPayout)
        .filter(BookingMemberPayout.booking_id == booking_id)
        .all()
    )
    return [serialize_payout(p) for p in payouts]


@router.put(
    "/bookings/{booking_id}/member-payouts",
    response_model=list[BookingMemberPayoutOut],
)
def upsert_member_payouts(
    booking_id: UUID,
    payload: BookingMemberPayoutUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    _assert_can_manage_member_payouts(db, booking, current_user)

    if booking.price_agreed is None:
        raise HTTPException(
            400,
            "La reserva no tiene precio acordado; no se puede repartir el pago.",
        )
    price_cap = Decimal(str(booking.price_agreed))

    existing_rows = (
        db.query(BookingMemberPayout)
        .filter(BookingMemberPayout.booking_id == booking_id)
        .all()
    )
    amount_by_member: dict[UUID, Decimal] = {
        row.ensemble_member_id: Decimal(str(row.amount or 0))
        for row in existing_rows
    }
    paid_members = {
        row.ensemble_member_id
        for row in existing_rows
        if row.status == BookingMemberPayoutStatus.paid
    }

    results: list[BookingMemberPayout] = []
    now = datetime.utcnow()
    for item in payload.items:
        member = get_member_for_leader(
            db, leader_id=current_user.id, member_id=item.ensemble_member_id
        )
        if member.id in paid_members:
            existing_paid = next(
                (r for r in existing_rows if r.ensemble_member_id == member.id),
                None,
            )
            if existing_paid:
                results.append(existing_paid)
            continue
        payout = (
            db.query(BookingMemberPayout)
            .filter(
                BookingMemberPayout.booking_id == booking_id,
                BookingMemberPayout.ensemble_member_id == member.id,
            )
            .first()
        )
        if not payout:
            payout = BookingMemberPayout(
                booking_id=booking_id,
                ensemble_member_id=member.id,
                currency="PEN",
            )
            db.add(payout)
        payout.amount = item.amount
        payout.note = item.note
        payout.set_by_leader_at = now
        if payload.lock:
            payout.status = BookingMemberPayoutStatus.locked
        elif payout.status != BookingMemberPayoutStatus.paid:
            payout.status = BookingMemberPayoutStatus.draft
        amount_by_member[member.id] = Decimal(str(item.amount))
        results.append(payout)

    total_assigned = sum(amount_by_member.values(), Decimal("0"))
    if total_assigned > price_cap:
        raise HTTPException(
            400,
            f"El reparto total (S/ {total_assigned:.2f}) no puede exceder "
            f"el precio del evento (S/ {price_cap:.2f}).",
        )

    db.commit()
    for payout in results:
        db.refresh(payout)
    return [serialize_payout(p) for p in results]


@router.post(
    "/bookings/{booking_id}/member-payouts/{payout_id}/mark-paid",
    response_model=BookingMemberPayoutOut,
)
def mark_payout_paid(
    booking_id: UUID,
    payout_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Reserva no encontrada")
    _assert_can_manage_member_payouts(db, booking, current_user)
    payout = (
        db.query(BookingMemberPayout)
        .filter(
            BookingMemberPayout.id == payout_id,
            BookingMemberPayout.booking_id == booking_id,
        )
        .first()
    )
    if not payout:
        raise HTTPException(404, "Reparto no encontrado")
    payout.status = BookingMemberPayoutStatus.paid
    payout.paid_at = datetime.utcnow()
    db.commit()
    db.refresh(payout)
    return serialize_payout(payout)


@router.get(
    "/musician/member-settlements",
    response_model=list[MemberSettlementBookingOut],
)
def list_member_settlements(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Reservas del líder con integrantes a pagar (módulo Pagos)."""
    musician = get_leader_musician_or_403(db, current_user)
    bookings = (
        db.query(Booking)
        .options(*booking_parties_load_options())
        .filter(Booking.musician_id == musician.id)
        .order_by(Booking.event_date.desc(), Booking.created_at.desc())
        .all()
    )
    if not bookings:
        return []

    booking_ids = [b.id for b in bookings]
    invites = (
        db.query(BookingMemberInvite)
        .options(joinedload(BookingMemberInvite.ensemble_member))
        .filter(BookingMemberInvite.booking_id.in_(booking_ids))
        .all()
    )
    payouts = (
        db.query(BookingMemberPayout)
        .options(joinedload(BookingMemberPayout.ensemble_member))
        .filter(BookingMemberPayout.booking_id.in_(booking_ids))
        .all()
    )
    reviews = {
        row[0]
        for row in db.query(ContractorRecommendation.booking_id)
        .filter(ContractorRecommendation.booking_id.in_(booking_ids))
        .all()
    }

    invites_by_booking: dict[UUID, list[BookingMemberInvite]] = {}
    for invite in invites:
        invites_by_booking.setdefault(invite.booking_id, []).append(invite)

    payouts_by_booking: dict[UUID, dict[UUID, BookingMemberPayout]] = {}
    for payout in payouts:
        payouts_by_booking.setdefault(payout.booking_id, {})[
            payout.ensemble_member_id
        ] = payout

    results: list[MemberSettlementBookingOut] = []
    for booking in bookings:
        booking_invites = invites_by_booking.get(booking.id, [])
        if not booking_invites:
            continue

        # Solo reservas con al menos un integrante aceptado (o pendiente con payout)
        accepted = [
            i
            for i in booking_invites
            if i.status == BookingMemberInviteStatus.accepted
        ]
        booking_payouts = payouts_by_booking.get(booking.id, {})
        if not accepted and not booking_payouts:
            continue

        has_review = booking.id in reviews
        can_pay = (
            booking.status in PAYOUT_ELIGIBLE_STATUSES and has_review
        )
        lines: list[MemberSettlementLineOut] = []
        total_assigned = Decimal("0")
        total_paid = Decimal("0")
        total_pending = Decimal("0")

        source_invites = accepted or [
            i
            for i in booking_invites
            if i.ensemble_member_id in booking_payouts
        ]
        seen_members: set[UUID] = set()
        for invite in source_invites:
            if invite.ensemble_member_id in seen_members:
                continue
            seen_members.add(invite.ensemble_member_id)
            member = invite.ensemble_member
            payout = booking_payouts.get(invite.ensemble_member_id)
            amount = Decimal(str(payout.amount)) if payout else Decimal("0")
            status = payout.status.value if payout else None
            if status == BookingMemberPayoutStatus.paid.value:
                total_paid += amount
            else:
                total_pending += amount
            total_assigned += amount
            lines.append(
                MemberSettlementLineOut(
                    ensemble_member_id=invite.ensemble_member_id,
                    member_fullname=member.fullname if member else "",
                    member_email=member.email if member else "",
                    invite_status=invite.status.value,
                    payout_id=payout.id if payout else None,
                    amount=amount,
                    currency=payout.currency if payout else "PEN",
                    payout_status=status,
                    paid_at=payout.paid_at if payout else None,
                )
            )

        # Incluir payouts huérfanos (sin invite accepted)
        for member_id, payout in booking_payouts.items():
            if member_id in seen_members:
                continue
            member = payout.ensemble_member
            amount = Decimal(str(payout.amount))
            status = payout.status.value
            if status == BookingMemberPayoutStatus.paid.value:
                total_paid += amount
            else:
                total_pending += amount
            total_assigned += amount
            lines.append(
                MemberSettlementLineOut(
                    ensemble_member_id=member_id,
                    member_fullname=member.fullname if member else "",
                    member_email=member.email if member else "",
                    invite_status="accepted",
                    payout_id=payout.id,
                    amount=amount,
                    currency=payout.currency or "PEN",
                    payout_status=status,
                    paid_at=payout.paid_at,
                )
            )

        results.append(
            MemberSettlementBookingOut(
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                start_time=booking.start_time,
                location_city=booking.location_city,
                status=booking.status.value,
                price_agreed=booking.price_agreed,
                can_pay=can_pay,
                has_contractor_review=has_review,
                members=lines,
                total_assigned=total_assigned,
                total_paid=total_paid,
                total_pending=total_pending,
            )
        )

    return results


@router.get("/musician/my-income", response_model=MyMemberIncomeSummary)
def list_my_member_income(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Ingresos del músico integrante (repartos asignados por el líder)."""
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "No autorizado")

    member_rows = (
        db.query(EnsembleMember)
        .filter(EnsembleMember.member_user_id == current_user.id)
        .all()
    )
    if not member_rows:
        return MyMemberIncomeSummary(
            total_assigned=Decimal("0"),
            total_pending=Decimal("0"),
            total_paid=Decimal("0"),
            items=[],
        )

    member_ids = [m.id for m in member_rows]
    leader_ids = {m.leader_user_id for m in member_rows}
    leaders = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(leader_ids)).all()
    }

    payouts = (
        db.query(BookingMemberPayout)
        .options(
            joinedload(BookingMemberPayout.ensemble_member),
            joinedload(BookingMemberPayout.booking),
        )
        .filter(BookingMemberPayout.ensemble_member_id.in_(member_ids))
        .order_by(BookingMemberPayout.created_at.desc())
        .all()
    )

    items: list[MyMemberIncomeItem] = []
    total_assigned = Decimal("0")
    total_pending = Decimal("0")
    total_paid = Decimal("0")

    for payout in payouts:
        booking = payout.booking
        if not booking:
            continue
        amount = Decimal(str(payout.amount or 0))
        status = (
            payout.status.value
            if hasattr(payout.status, "value")
            else str(payout.status)
        )
        total_assigned += amount
        if status == BookingMemberPayoutStatus.paid.value:
            total_paid += amount
        else:
            total_pending += amount

        member = payout.ensemble_member
        leader = leaders.get(member.leader_user_id) if member else None
        items.append(
            MyMemberIncomeItem(
                payout_id=payout.id,
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                booking_status=booking.status.value,
                leader_name=leader.fullname if leader else None,
                amount=amount,
                currency=payout.currency or "PEN",
                status=status,
                note=payout.note,
                set_by_leader_at=payout.set_by_leader_at,
                paid_at=payout.paid_at,
            )
        )

    return MyMemberIncomeSummary(
        total_assigned=total_assigned,
        total_pending=total_pending,
        total_paid=total_paid,
        items=items,
    )


@router.get("/musician/my-calls", response_model=list[BookingMemberInviteOut])
def list_my_calls(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Convocatorias del integrante autenticado."""
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "No autorizado")
    member_ids = [
        row.id
        for row in db.query(EnsembleMember.id)
        .filter(EnsembleMember.member_user_id == current_user.id)
        .all()
    ]
    if not member_ids:
        return []
    invites = (
        db.query(BookingMemberInvite)
        .filter(BookingMemberInvite.ensemble_member_id.in_(member_ids))
        .order_by(BookingMemberInvite.invited_at.desc())
        .all()
    )
    return [serialize_booking_invite(i) for i in invites]

