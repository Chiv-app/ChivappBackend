"""Reservas iniciadas por el músico para un cliente (cotizaciones).

La reserva queda como cotización aceptable por el cliente: él mismo acepta,
firma y paga por Mercado Pago. El músico no puede firmar ni validar pagos.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

def get_lima_today() -> date:
    return datetime.now(ZoneInfo("America/Lima")).date()

from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.schemas.booking import MusicianBookingCreate
from app.services.availability_match import assert_musician_available
from app.services.booking_contract import create_booking_contract
from app.services.contract_pdf import contract_html_text_length
from app.services.uploads import upload_exists
from app.services.uniqueness import (
    assert_document_number_unique,
    assert_phone_unique,
    normalize_document_number,
    normalize_email,
    normalize_phone,
)


def assert_upload_exists(upload_url: str, label: str) -> None:
    if not upload_exists(upload_url):
        raise HTTPException(
            status_code=400,
            detail=f"No se encontró el archivo de {label}. Vuelve a subirlo.",
        )


def resolve_or_create_contractor_client(
    db: Session,
    *,
    fullname: str,
    email: str | None,
    phone: str | None,
    document_type: str | None,
    document_number: str | None,
    address: str | None,
    city: str | None,
    musician_id=None,
) -> tuple[ContractorProfile, User]:
    email_raw = normalize_email(email)
    has_real_email = bool(email_raw)
    email_norm = email_raw if has_real_email else f"cliente-{uuid4().hex[:12]}@guest.local"

    user = None
    if has_real_email:
        user = (
            db.query(User)
            .filter(func.lower(User.email) == email_norm)
            .first()
        )
    else:
        phone_clean = normalize_phone(phone)
        if phone_clean and musician_id is not None:
            user = (
                db.query(User)
                .filter(
                    User.role == UserRole.contractor,
                    User.email.like("%@guest.local"),
                    User.phone == phone_clean,
                    User.created_by_musician_id == musician_id,
                )
                .first()
            )
        if user is None and document_number and musician_id is not None:
            doc_clean = normalize_document_number(document_number)
            if doc_clean:
                existing_prof = (
                    db.query(ContractorProfile)
                    .join(User, ContractorProfile.user_id == User.id)
                    .filter(
                        User.role == UserRole.contractor,
                        User.email.like("%@guest.local"),
                        ContractorProfile.document_number == doc_clean,
                        User.created_by_musician_id == musician_id,
                    )
                    .first()
                )
                if existing_prof:
                    user = db.get(User, existing_prof.user_id)

    if user:
        if user.role != UserRole.contractor:
            raise HTTPException(
                status_code=400,
                detail="No se pudo crear la cotización con los datos proporcionados",
            )
        profile = (
            db.query(ContractorProfile)
            .filter(ContractorProfile.user_id == user.id)
            .first()
        )
        if not profile:
            profile = ContractorProfile(
                user_id=user.id,
                status=ProfileStatus.draft,
            )
            db.add(profile)
            db.flush()

        if not has_real_email:
            # Cuenta walk-in (@guest.local) creada por este mismo músico:
            # completar solo datos faltantes.
            if fullname and fullname.strip() and not (user.fullname or "").strip():
                user.fullname = fullname.strip()
            if phone and not user.phone:
                user.phone = normalize_phone(phone)
            if document_type and not profile.document_type:
                profile.document_type = document_type
            if address and not profile.address:
                profile.address = address
            if city and not profile.city:
                profile.city = city
            db.flush()
        else:
            # S-01 (WP-08): Para cuentas reales ya registradas, no mutar ni exponer
            # los datos privados del perfil del contratista hasta que él acepte.
            profile._reused_existing_account = True
        return profile, user

    phone_norm = (
        assert_phone_unique(db, phone)
        if has_real_email
        else normalize_phone(phone)
    )
    user = User(
        email=email_norm,
        fullname=fullname.strip(),
        phone=phone_norm,
        role=UserRole.contractor,
        password_hash=None,
        is_verified=False,
        # Sin correo real: cuenta solo de soporte a la contrata (sin login útil)
        is_active=has_real_email,
        created_by_musician_id=musician_id if not has_real_email else None,
    )
    db.add(user)
    db.flush()

    # SEC4-08 / SEC4-13: una cuenta sin verificar (o walk-in @guest.local) no
    # reserva el DNI en el índice único de contractor_profile.
    if document_number:
        norm_doc = normalize_document_number(document_number)
        if norm_doc:
            existing_verified = (
                db.query(ContractorProfile)
                .join(User, ContractorProfile.user_id == User.id)
                .filter(
                    func.upper(func.replace(func.btrim(ContractorProfile.document_number), " ", ""))
                    == norm_doc,
                )
                .first()
            )
            if existing_verified:
                owner = db.get(User, existing_verified.user_id)
                if owner is not None and (owner.is_verified or owner.email_verified_at is not None):
                    raise HTTPException(
                        status_code=400,
                        detail="Ya existe una cuenta con ese número de documento (DNI)",
                    )
    profile = ContractorProfile(
        user_id=user.id,
        document_type=document_type,
        document_number=None,
        address=address,
        city=city,
        status=ProfileStatus.draft,
    )
    db.add(profile)
    db.flush()
    return profile, user


def create_musician_booking(
    db: Session,
    *,
    musician: MusicianProfile,
    musician_user: User,
    payload: MusicianBookingCreate,
    sign_ip: str = "unknown",
) -> Booking:
    if musician.status != ProfileStatus.published or not musician_user.is_verified:
        raise HTTPException(
            status_code=403,
            detail="Tu perfil de músico debe estar verificado para crear contratas",
        )

    if not (musician.signature_image_url or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Debes registrar tu firma digital antes de crear contratas",
        )

    if contract_html_text_length(musician.contract_template_body) < 50:
        if payload.already_paid:
            from app.services.contract_pdf import (
                DEFAULT_CONTRACT_TEMPLATE_TITLE,
                build_default_contract_template_body,
            )

            musician.contract_template_title = (
                musician.contract_template_title or DEFAULT_CONTRACT_TEMPLATE_TITLE
            )
            musician.contract_template_body = build_default_contract_template_body()
            db.flush()
        else:
            raise HTTPException(
                status_code=400,
                detail="Configura tu plantilla de contrato antes de crear contratas",
            )

    if payload.event_date < get_lima_today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")
    if (
        payload.event_date == get_lima_today()
        and payload.start_time < datetime.now(ZoneInfo("America/Lima")).time()
    ):
        raise HTTPException(400, "La hora del evento ya pasó")

    # WP-13 (BE-05): Serializar creación de contrata bloqueando fila de MusicianProfile.
    db.get(MusicianProfile, musician.id, with_for_update=True)
    assert_musician_available(
        db,
        musician.id,
        payload.event_date,
        payload.start_time,
        end_time=payload.end_time,
    )

    contractor, contractor_user = resolve_or_create_contractor_client(
        db,
        fullname=payload.contractor_fullname or f"Cliente ({payload.contractor_phone or ''})",
        email=str(payload.contractor_email) if payload.contractor_email else None,
        phone=payload.contractor_phone,
        document_type=payload.document_type,
        document_number=payload.document_number,
        address=payload.contractor_address,
        city=payload.contractor_city,
        musician_id=musician.id,
    )

    initial_status = (
        BookingStatus.payment_retained
        if payload.already_paid
        else BookingStatus.accepted
    )

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=musician.id,
        event_date=payload.event_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        location_address=payload.location_address.strip(),
        location_city=payload.location_city,
        location_reference=payload.location_reference,
        event_type=payload.event_type.strip(),
        event_description=payload.event_description,
        requested_repertoire=payload.requested_repertoire,
        price_agreed=payload.price_agreed,
        musician_quote_notes=payload.musician_quote_notes,
        quoted_at=datetime.utcnow(),
        status=initial_status,
    )
    db.add(booking)
    db.flush()

    from types import SimpleNamespace
    from app.services.platform_payment import sync_booking_platform_fee

    if payload.already_paid:
        booking.platform_fee_percent = Decimal("0.00")
        booking.platform_fee_amount = Decimal("0.00")
    else:
        sync_booking_platform_fee(db, booking, use_current_settings=True)

    transient_doc = normalize_document_number(payload.document_number)
    reused_existing = bool(getattr(contractor, "_reused_existing_account", False))
    keep_doc_in_profile = bool(
        not reused_existing
        and contractor.document_number
        and (contractor_user.is_verified or contractor_user.email_verified_at is not None)
    )
    try:
        with db.no_autoflush:
            if reused_existing:
                # S-01 (WP-08): No volcar datos guardados del perfil de una cuenta existente
                # al contrato hasta que el propio contratista acepte la cotización.
                contract_contractor = SimpleNamespace(
                    id=contractor.id,
                    user_id=contractor.user_id,
                    document_type=payload.document_type,
                    document_number=transient_doc,
                    address=payload.contractor_address,
                    city=payload.contractor_city,
                )
                contract_user = SimpleNamespace(
                    id=contractor_user.id,
                    fullname=(payload.contractor_fullname or "").strip() or "Cliente",
                    email=str(payload.contractor_email) if payload.contractor_email else contractor_user.email,
                    phone=normalize_phone(payload.contractor_phone) if payload.contractor_phone else None,
                )
                contract = create_booking_contract(
                    db,
                    booking,
                    musician,
                    musician_user,
                    contract_contractor,
                    contract_user,
                )
                contract.context = {
                    **(contract.context or {}),
                    "__musician_created_unaccepted__": True,
                }
            else:
                if not keep_doc_in_profile and transient_doc:
                    contractor.document_number = transient_doc
                contract = create_booking_contract(
                    db,
                    booking,
                    musician,
                    musician_user,
                    contractor,
                    contractor_user,
                )
                if not keep_doc_in_profile:
                    contractor.document_number = None
            if payload.already_paid and contract is not None:
                contract.context = {
                    **(contract.context or {}),
                    "__already_paid_external__": True,
                }
    except ValueError as exc:
        if not reused_existing and not keep_doc_in_profile:
            contractor.document_number = None
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if (
        payload.already_paid
        and payload.ensemble_member_ids
        and (musician.profile_type or "solo") == "group"
    ):
        from app.api.v1.endpoints.ensemble_members import _generate_ics_attachment
        from app.core.config import settings
        from app.models.ensemble_member import (
            BookingMemberInvite,
            BookingMemberInviteStatus,
            EnsembleMember,
            EnsembleMemberStatus,
        )
        from app.services.booking_notifications import notify_user
        from app.services.email.auth_emails import send_booking_member_invite_email
        from app.services.ensemble_members import (
            _hash_setup_token,
            booking_respond_url,
            is_leader_managed_account,
            new_token,
        )
        from app.services.musician_profiles import member_belongs_to_profile

        group_label = (
            (musician.stage_name or "").strip()
            or (musician_user.fullname or "").strip()
            or "Tu agrupación"
        )
        event_time = str(booking.start_time or "Por confirmar")
        if booking.end_time:
            event_time = f"{booking.start_time or '?'} - {booking.end_time}"
        location_parts = [
            part for part in (booking.location_address, booking.location_city) if part
        ]
        event_location = ", ".join(location_parts) or "Por confirmar"
        ics_file = _generate_ics_attachment(booking, group_label)
        extra_atts = [ics_file] if ics_file else None

        seen_member_ids = set()
        for member_id in payload.ensemble_member_ids:
            if member_id in seen_member_ids:
                continue
            seen_member_ids.add(member_id)
            member = (
                db.query(EnsembleMember)
                .filter(
                    EnsembleMember.id == member_id,
                    EnsembleMember.leader_user_id == musician_user.id,
                )
                .first()
            )
            if (
                not member
                or member.status == EnsembleMemberStatus.inactive
                or not member_belongs_to_profile(member, musician)
            ):
                continue
            linked_user = db.get(User, member.member_user_id) if member.member_user_id else None
            raw_token = new_token()
            token_hash = _hash_setup_token(raw_token)
            invite = BookingMemberInvite(
                booking_id=booking.id,
                ensemble_member_id=member.id,
                status=BookingMemberInviteStatus.pending,
                response_token=token_hash,
                invited_at=datetime.utcnow(),
            )
            db.add(invite)
            db.flush()
            if linked_user:
                notify_user(
                    db,
                    user=linked_user,
                    type="booking_member_invite",
                    title=f"Nueva convocatoria: {booking.event_type}",
                    message=(
                        f"{group_label} te convocó para {booking.event_type} "
                        f"el {booking.event_date} a las {str(booking.start_time)[:5]}."
                    ),
                    booking_id=str(booking.id),
                    meta={"ensemble_member_id": str(member.id)},
                )
            if member.email and not member.email.endswith("@guest.local"):
                respond_url = booking_respond_url(raw_token)
                final_respond_url = respond_url or f"{settings.FRONTEND_URL.rstrip('/')}/musician"
                send_booking_member_invite_email(
                    db,
                    member_email=member.email,
                    member_name=member.fullname,
                    leader_name=group_label,
                    event_type=booking.event_type,
                    event_date=str(booking.event_date),
                    event_time=event_time,
                    event_location=event_location,
                    respond_url=final_respond_url,
                    user_id=member.member_user_id,
                    booking_id=str(booking.id),
                    extra_attachments=extra_atts,
                )

    db.commit()
    db.refresh(booking)

    if payload.already_paid:
        try:
            from app.services.calendar_service import sync_booking_calendar

            sync_booking_calendar(
                db,
                str(booking.id),
                include_contractor=False,
                ensemble_member_ids=[str(m) for m in (payload.ensemble_member_ids or [])],
            )
        except Exception:
            pass

    return booking

