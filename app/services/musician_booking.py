"""Reservas iniciadas por el músico para un cliente (propuestas).

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
from app.services.booking_contract import create_booking_contract
from app.services.contract_pdf import contract_html_text_length
from app.services.uploads import upload_exists
from app.services.uniqueness import (
    assert_document_number_unique,
    assert_phone_unique,
    normalize_email,
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

    if user:
        if user.role != UserRole.contractor:
            raise HTTPException(
                status_code=400,
                detail="Ese correo ya pertenece a una cuenta que no es de contratista",
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

        # Completa solo datos faltantes: la cuenta es del cliente, el músico no
        # puede sobrescribir su información.
        if fullname and fullname.strip() and not (user.fullname or "").strip():
            user.fullname = fullname.strip()
        if phone and not user.phone:
            user.phone = assert_phone_unique(db, phone, exclude_user_id=user.id)
        if document_type and not profile.document_type:
            profile.document_type = document_type
        if document_number and not profile.document_number:
            profile.document_number = assert_document_number_unique(
                db,
                document_number,
                exclude_profile_id=profile.id,
            )
        if address and not profile.address:
            profile.address = address
        if city and not profile.city:
            profile.city = city
        db.flush()
        return profile, user

    phone_norm = assert_phone_unique(db, phone)
    user = User(
        email=email_norm,
        fullname=fullname.strip(),
        phone=phone_norm,
        role=UserRole.contractor,
        password_hash=None,
        is_verified=False,
        # Sin correo real: cuenta solo de soporte a la contrata (sin login útil)
        is_active=has_real_email,
    )
    db.add(user)
    db.flush()

    doc_norm = assert_document_number_unique(db, document_number)
    profile = ContractorProfile(
        user_id=user.id,
        document_type=document_type,
        document_number=doc_norm,
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

    if contract_html_text_length(musician.contract_template_body) < 50:
        raise HTTPException(
            status_code=400,
            detail="Configura tu plantilla de contrato antes de crear contratas",
        )

    if payload.event_date < get_lima_today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")

    contractor, contractor_user = resolve_or_create_contractor_client(
        db,
        fullname=payload.contractor_fullname,
        email=str(payload.contractor_email) if payload.contractor_email else None,
        phone=payload.contractor_phone,
        document_type=payload.document_type,
        document_number=payload.document_number,
        address=payload.contractor_address,
        city=payload.contractor_city,
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
        price_agreed=payload.price_agreed,
        musician_quote_notes=payload.musician_quote_notes,
        quoted_at=datetime.utcnow(),
        status=BookingStatus.accepted,
    )
    db.add(booking)
    db.flush()

    from app.services.platform_payment import sync_booking_platform_fee

    sync_booking_platform_fee(db, booking, use_current_settings=True)

    try:
        create_booking_contract(
            db,
            booking,
            musician,
            musician_user,
            contractor,
            contractor_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    db.refresh(booking)
    return booking

