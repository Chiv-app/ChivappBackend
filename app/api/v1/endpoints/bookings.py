from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_contractor_owner,
    assert_booking_musician_owner,
    assert_booking_participant,
    assert_booking_viewer,
    booking_parties_load_options,
    get_booking_or_404,
    get_contractor_profile_or_400,
    get_musician_profile_or_400,
    list_member_booking_ids,
    parse_uuid,
    resolve_member_invite_status,
    serialize_booking_out,
)
from app.api.profile_helpers import get_or_create_contractor_profile
from app.models.user import User, UserRole
from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.schemas.booking import (
    BookingConfirm,
    BookingCreate,
    BookingOut,
    BookingQuote,
    BookingReject,
    BookingReopenQuote,
    BookingRequestedRepertoireUpdate,
    BookingUpdate,
    MusicianAttachContractorSignature,
    MusicianBookingCreate,
    BookingCalendarSyncRequest,
)
from app.services.availability_match import assert_musician_available
from app.services.booking_contract import create_booking_contract
from app.services.payment_evidence import (
    apply_evidence_urls,
    assert_evidence_uploads,
    normalize_evidence_urls,
)
from app.services.booking_notifications import (
    notify_admins_payment_submitted,
    notify_booking_cancelled,
    notify_booking_confirmed,
    notify_booking_created,
    notify_booking_quote_accepted,
    notify_booking_quote_updated,
    notify_booking_quoted,
    notify_booking_rejected,
    notify_booking_updated,
)
from app.services.musician_booking import (
    attach_contractor_signature_as_musician,
    create_musician_booking,
)
from app.services.uploads import UPLOAD_DIR


router = APIRouter(prefix="/bookings", tags=["Bookings"])


def _get_booking_with_parties(db: Session, booking_id: str) -> Booking:
    booking = (
        db.query(Booking)
        .options(*booking_parties_load_options())
        .filter(Booking.id == parse_uuid(booking_id, "booking_id"))
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking no encontrado")
    return booking


def _load_musician_user(db: Session, musician: MusicianProfile) -> User:
    return db.query(User).filter(User.id == musician.user_id).first()


def _load_contractor_user(db: Session, contractor_id) -> User:
    from app.models.contractor_profile import ContractorProfile

    contractor = db.query(ContractorProfile).filter(
        ContractorProfile.id == contractor_id
    ).first()
    if not contractor:
        raise HTTPException(404, "Contratista no encontrado")
    user = db.query(User).filter(User.id == contractor.user_id).first()
    if not user:
        raise HTTPException(404, "Usuario contratista no encontrado")
    return user


@router.post("", response_model=BookingOut, status_code=201)
@router.post("/", response_model=BookingOut, status_code=201, include_in_schema=False)
def create_booking(
    payload: BookingCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(403, "Solo los contratistas pueden crear reservas")

    contractor = get_or_create_contractor_profile(db, current_user)
    if not current_user.is_active:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta de contratista está desactivada",
        )

    if not current_user.email_verified_at:
        raise HTTPException(
            status_code=403,
            detail="Confirma tu correo electrónico antes de crear reservas",
        )

    if not current_user.is_verified:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta de contratista no ha sido verificada. Completa el paso de Datos en tu perfil.",
        )

    musician = (
        db.query(MusicianProfile)
        .join(User)
        .options(joinedload(MusicianProfile.user))
        .filter(
            MusicianProfile.id == payload.musician_id,
            MusicianProfile.status == ProfileStatus.published,
            User.is_verified.is_(True),
        )
        .first()
    )

    if not musician:
        raise HTTPException(404, "El músico no existe o no está verificado")

    if payload.event_date < date.today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")

    assert_musician_available(
        db,
        musician.id,
        payload.event_date,
        payload.start_time,
    )

    booking = Booking(
        contractor_id=contractor.id,
        musician_id=payload.musician_id,
        event_date=payload.event_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        location_address=payload.location_address,
        location_city=payload.location_city,
        location_reference=payload.location_reference,
        event_type=payload.event_type,
        event_description=payload.event_description,
        status=BookingStatus.requested,
    )

    db.add(booking)
    db.commit()
    db.refresh(booking)

    musician_user = musician.user or _load_musician_user(db, musician)
    if musician_user:
        notify_booking_created(db, musician_user, str(booking.id))
        db.commit()

    return booking


@router.post("/musician-created", response_model=BookingOut, status_code=201)
def create_booking_as_musician(
    payload: MusicianBookingCreate,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """El músico crea una contrata con cliente, montos y firma opcional."""
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo los músicos pueden crear contratas desde este flujo")

    musician = get_musician_profile_or_400(db, current_user, require_verified=True)
    booking = create_musician_booking(
        db,
        musician=musician,
        musician_user=current_user,
        payload=payload,
        sign_ip=_client_ip(request),
    )

    # Notifica al contratista si ya tiene cuenta activa
    try:
        contractor_user = _load_contractor_user(db, booking.contractor_id)
        if contractor_user and contractor_user.is_active:
            notify_booking_created(db, contractor_user, str(booking.id))
            db.commit()
    except HTTPException:
        pass

    return booking


@router.post(
    "/{booking_id}/attach-contractor-signature",
    response_model=BookingOut,
)
def attach_contractor_signature(
    booking_id: str,
    payload: MusicianAttachContractorSignature,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """El músico adjunta la firma del contratista para regularizar el contrato."""
    booking = get_booking_or_404(db, booking_id)
    musician = assert_booking_musician_owner(db, booking, current_user)
    return attach_contractor_signature_as_musician(
        db,
        booking=booking,
        musician=musician,
        musician_user=current_user,
        payload=payload,
        sign_ip=_client_ip(request, payload.sign_ip),
    )


@router.get("", response_model=list[BookingOut])
@router.get("/", response_model=list[BookingOut], include_in_schema=False)
def list_bookings(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role == UserRole.contractor:
        contractor = get_contractor_profile_or_400(db, current_user)
        bookings = (
            db.query(Booking)
            .options(*booking_parties_load_options())
            .filter(Booking.contractor_id == contractor.id)
            .order_by(Booking.created_at.desc())
            .all()
        )
        return [
            serialize_booking_out(b, viewer_role="owner") for b in bookings
        ]

    if current_user.role == UserRole.musician:
        musician = get_musician_profile_or_400(db, current_user)
        owned = (
            db.query(Booking)
            .options(*booking_parties_load_options())
            .filter(Booking.musician_id == musician.id)
            .all()
        )
        owned_ids = {b.id for b in owned}

        member_ids = list_member_booking_ids(db, current_user)
        member_only_ids = [bid for bid in member_ids if bid not in owned_ids]
        member_bookings: list[Booking] = []
        if member_only_ids:
            member_bookings = (
                db.query(Booking)
                .options(*booking_parties_load_options())
                .filter(Booking.id.in_(member_only_ids))
                .all()
            )

        results = [
            serialize_booking_out(b, viewer_role="owner") for b in owned
        ] + [
            serialize_booking_out(
                b,
                viewer_role="member",
                member_invite_status=resolve_member_invite_status(
                    db, b, current_user
                ),
            )
            for b in member_bookings
        ]
        results.sort(key=lambda row: row.created_at, reverse=True)
        return results

    return []


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = _get_booking_with_parties(db, booking_id)
    viewer_role = assert_booking_viewer(db, booking, current_user)
    invite_status = (
        resolve_member_invite_status(db, booking, current_user)
        if viewer_role == "member"
        else None
    )
    return serialize_booking_out(
        booking,
        viewer_role=viewer_role,
        member_invite_status=invite_status,
    )


@router.put("/{booking_id}", response_model=BookingOut)
def update_booking(
    booking_id: str,
    payload: BookingUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status != BookingStatus.requested:
        raise HTTPException(
            status_code=400,
            detail="Solo puedes editar solicitudes en estado 'requested'",
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(booking, field, value)

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if musician:
        musician_user = _load_musician_user(db, musician)
        if musician_user:
            notify_booking_updated(db, musician_user, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/reject", response_model=BookingOut)
def reject_booking(
    booking_id: str,
    payload: BookingReject,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_musician_owner(db, booking, current_user)

    if booking.status != BookingStatus.requested:
        raise HTTPException(
            status_code=400,
            detail="Solo puedes rechazar solicitudes en estado 'requested'",
        )

    booking.status = BookingStatus.cancelled
    booking.cancelled_by = "musician"
    booking.rejection_reason = payload.rejection_reason or "Solicitud rechazada por el músico"

    contractor_user = _load_contractor_user(db, booking.contractor_id)
    notify_booking_rejected(
        db,
        contractor_user,
        str(booking.id),
        by_role="musician",
    )

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/quote", response_model=BookingOut)
def quote_booking(
    booking_id: str,
    payload: BookingQuote,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    musician = assert_booking_musician_owner(db, booking, current_user)

    if booking.status not in (BookingStatus.requested, BookingStatus.accepted):
        raise HTTPException(
            status_code=400,
            detail="Solo puedes cotizar solicitudes pendientes o actualizar cotizaciones aún no aceptadas",
        )

    is_update = booking.status == BookingStatus.accepted

    if not musician.contract_template_body or len(musician.contract_template_body.strip()) < 50:
        raise HTTPException(
            status_code=400,
            detail="Configura tu plantilla de contrato antes de enviar cotizaciones",
        )

    booking.price_agreed = payload.price_agreed
    booking.advance_amount = payload.price_agreed  # 100% upfront payment
    booking.musician_quote_notes = payload.musician_quote_notes
    booking.quoted_at = datetime.utcnow()
    booking.status = BookingStatus.accepted

    from app.services.platform_payment import sync_booking_platform_fee

    sync_booking_platform_fee(db, booking, use_current_settings=True)

    if payload.location_address:
        booking.location_address = payload.location_address
    if payload.location_city:
        booking.location_city = payload.location_city
    if payload.location_reference:
        booking.location_reference = payload.location_reference

    contractor_user = _load_contractor_user(db, booking.contractor_id)
    if is_update:
        notify_booking_quote_updated(db, contractor_user, str(booking.id))
    else:
        notify_booking_quoted(db, contractor_user, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/reject-quote", response_model=BookingOut)
def reject_quote(
    booking_id: str,
    payload: BookingReject,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status != BookingStatus.accepted:
        raise HTTPException(
            status_code=400,
            detail="Solo puedes rechazar cotizaciones en estado 'accepted'",
        )

    booking.status = BookingStatus.cancelled
    booking.cancelled_by = "contractor"
    booking.rejection_reason = payload.rejection_reason or "Cotización rechazada por el contratista"

    musician = db.query(MusicianProfile).filter(
        MusicianProfile.id == booking.musician_id
    ).first()
    if musician:
        musician_user = _load_musician_user(db, musician)
        if musician_user:
            notify_booking_rejected(
                db,
                musician_user,
                str(booking.id),
                by_role="contractor",
            )

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/accept-quote", response_model=BookingOut)
def accept_quote(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    contractor = assert_booking_contractor_owner(db, booking, current_user)

    if booking.status != BookingStatus.accepted:
        raise HTTPException(
            status_code=400,
            detail="Solo puedes aceptar cotizaciones en estado 'accepted'",
        )

    musician = db.query(MusicianProfile).filter(
        MusicianProfile.id == booking.musician_id
    ).first()
    if not musician:
        raise HTTPException(404, "Músico no encontrado")

    musician_user = _load_musician_user(db, musician)
    contractor_user = current_user

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

    notify_booking_quote_accepted(db, musician_user, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


def _musician_repertoire_titles(musician: MusicianProfile) -> set[str]:
    titles: set[str] = set()
    for song in musician.songs or []:
        if song and str(song).strip():
            titles.add(str(song).strip())
    for item in musician.repertoire or []:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            if title:
                titles.add(title)
        elif item:
            titles.add(str(item).strip())
    return titles


def _normalize_other_title(title: str) -> str | None:
    """Permite temas especiales con prefijo 'Otros:'."""
    raw = (title or "").strip()
    if not raw.lower().startswith("otros:"):
        return None
    custom = raw.split(":", 1)[-1].strip()
    if not custom:
        return None
    return f"Otros: {custom[:140]}"


def _set_requested_repertoire(
    booking: Booking,
    musician: MusicianProfile,
    titles: list[str],
) -> None:
    allowed = _musician_repertoire_titles(musician)
    cleaned: list[str] = []
    seen: set[str] = set()
    for title in titles:
        normalized_other = _normalize_other_title(title)
        if normalized_other:
            value = normalized_other
        elif title in allowed:
            value = title
        else:
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    booking.requested_repertoire = cleaned


@router.put("/{booking_id}/requested-repertoire", response_model=BookingOut)
def update_requested_repertoire(
    booking_id: str,
    payload: BookingRequestedRepertoireUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Contratista elige temas del repertorio en la fase de contrato."""
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status not in (
        BookingStatus.accepted,
        BookingStatus.contract_pending,
    ):
        raise HTTPException(
            status_code=400,
            detail="Solo puedes elegir repertorio antes de confirmar el pago",
        )

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if not musician:
        raise HTTPException(404, "Músico no encontrado")

    _set_requested_repertoire(booking, musician, payload.requested_repertoire)
    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/reopen-quote", response_model=BookingOut)
def reopen_quote(
    booking_id: str,
    payload: BookingReopenQuote,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Edita la solicitud y vuelve a cotización (borra contrato/precio)."""
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status not in (
        BookingStatus.accepted,
        BookingStatus.contract_pending,
    ):
        raise HTTPException(
            status_code=400,
            detail="Solo puedes editar y reabrir cotización en estados accepted o contract_pending",
        )

    data = payload.model_dump(exclude_unset=True)
    repertoire = data.pop("requested_repertoire", None)
    for field, value in data.items():
        setattr(booking, field, value)

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if repertoire is not None and musician:
        _set_requested_repertoire(booking, musician, repertoire)

    assert_musician_available(
        db,
        booking.musician_id,
        booking.event_date,
        booking.start_time,
    )

    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if contract:
        db.delete(contract)

    booking.price_agreed = None
    booking.advance_amount = None
    booking.platform_fee_percent = None
    booking.platform_fee_amount = None
    booking.musician_quote_notes = None
    booking.quoted_at = None
    booking.status = BookingStatus.requested

    if musician:
        musician_user = _load_musician_user(db, musician)
        if musician_user:
            notify_booking_updated(db, musician_user, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


def _client_ip(request: Request, fallback: str | None = None) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return fallback or "unknown"


def _assert_upload_exists(upload_url: str, label: str) -> None:
    filename = upload_url.rsplit("/", 1)[-1]
    if not filename or not (UPLOAD_DIR / filename).is_file():
        raise HTTPException(
            status_code=400,
            detail=f"No se encontró el archivo de {label}. Vuelve a subirlo.",
        )


@router.post("/{booking_id}/confirm", response_model=BookingOut)
def confirm_booking(
    booking_id: str,
    payload: BookingConfirm,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status != BookingStatus.contract_pending:
        raise HTTPException(
            status_code=400,
            detail="La reserva debe estar en estado 'contract_pending'",
        )

    if not payload.terms_accepted:
        raise HTTPException(
            status_code=400,
            detail="Debes aceptar los términos del contrato del músico",
        )

    if not payload.signature_image_url:
        raise HTTPException(
            status_code=400,
            detail="Debes firmar el contrato para aceptar los términos",
        )

    _assert_upload_exists(payload.signature_image_url, "firma")
    evidence_urls = assert_evidence_uploads(
        normalize_evidence_urls(
            payment_evidence_url=payload.payment_evidence_url,
            payment_evidence_urls=payload.payment_evidence_urls,
        ),
        require_at_least_one=False,
    )

    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if not contract:
        raise HTTPException(404, "Contrato no encontrado para esta reserva")

    musician = db.query(MusicianProfile).filter(
        MusicianProfile.id == booking.musician_id
    ).first()
    if not musician:
        raise HTTPException(404, "Músico no encontrado")
    musician_user = _load_musician_user(db, musician)
    if not musician_user:
        raise HTTPException(404, "Usuario del músico no encontrado")

    now = datetime.utcnow()
    sign_ip = _client_ip(request, payload.sign_ip)

    has_snapshot = bool(contract.title and contract.body)
    has_legacy_pdf = bool(contract.contract_pdf_url or contract.contract_signed_pdf_url)
    if not has_snapshot and not has_legacy_pdf:
        raise HTTPException(
            status_code=400,
            detail=(
                "El contrato de esta reserva no tiene contenido. "
                "Pide al músico que vuelva a generar el acuerdo."
            ),
        )

    contract.terms_accepted = True
    contract.terms_accepted_at = now
    contract.terms_accepted_ip = sign_ip
    contract.contractor_signed = True
    contract.contractor_sign_timestamp = now
    contract.contractor_sign_ip = sign_ip
    contract.contractor_signature_url = payload.signature_image_url
    if not contract.musician_signature_url and musician.signature_image_url:
        contract.musician_signature_url = musician.signature_image_url
    # Con snapshot, el PDF firmado se genera on-demand; no se persiste archivo.
    if has_snapshot:
        contract.contract_signed_pdf_url = None

    payment = Payment(
        booking_id=booking.id,
        amount=payload.amount,
        payment_type=payload.payment_type,
        status=PaymentStatus.initiated,
    )
    apply_evidence_urls(payment, evidence_urls)

    booking.status = BookingStatus.contract_signed
    db.add(payment)
    db.flush()
    booking.status = BookingStatus.payment_pending

    notify_booking_confirmed(db, musician_user, str(booking.id))
    notify_admins_payment_submitted(db, str(booking.id), kind="advance")

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
    payload: BookingReject | None = None,
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    if booking.status in [
        BookingStatus.completed,
        BookingStatus.payment_released,
        BookingStatus.contract_signed,
        BookingStatus.payment_pending,
        BookingStatus.payment_retained,
    ]:
        raise HTTPException(400, "No se puede cancelar en este estado")

    cancelled_by = "musician" if current_user.role == UserRole.musician else "contractor"
    booking.cancelled_by = cancelled_by

    if payload and payload.rejection_reason:
        booking.rejection_reason = payload.rejection_reason

    booking.status = BookingStatus.cancelled

    from app.services.booking_share import disable_booking_share

    disable_booking_share(booking)

    other_user = None
    if cancelled_by == "musician":
        other_user = _load_contractor_user(db, booking.contractor_id)
    else:
        musician = (
            db.query(MusicianProfile)
            .filter(MusicianProfile.id == booking.musician_id)
            .first()
        )
        if musician:
            other_user = _load_musician_user(db, musician)

    if other_user:
        notify_booking_cancelled(
            db,
            other_user,
            str(booking.id),
            by_role=cancelled_by,
        )

    db.commit()
    db.refresh(booking)
    return booking

@router.post("/{booking_id}/calendar-sync", response_model=BookingOut)
def sync_booking_calendar_endpoint(
    booking_id: str,
    payload: BookingCalendarSyncRequest,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from app.services.calendar_service import sync_booking_calendar
    
    booking = _get_booking_with_parties(db, booking_id)
    viewer_role = assert_booking_viewer(db, booking, current_user)
    
    if viewer_role != "owner":
        raise HTTPException(403, "Solo el m\u00fasico l\u00edder puede sincronizar la agenda.")

    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo los m\u00fasicos pueden realizar esta acci\u00f3n.")

    str_member_ids = [str(uid) for uid in payload.ensemble_member_ids]
    new_event_id = sync_booking_calendar(db, str(booking.id), payload.include_contractor, str_member_ids)
    if new_event_id:
        booking.calendar_event_id = new_event_id
        db.commit()
    
    return serialize_booking_out(
        booking,
        viewer_role=viewer_role,
        member_invite_status=None,
    )



