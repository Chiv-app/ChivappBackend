import logging
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

def get_lima_today() -> date:
    return datetime.now(ZoneInfo("America/Lima")).date()


from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.core.config import settings
from app.schemas.booking_link import CancellationPolicyPublicOut
from app.services.booking_lifecycle import is_pre_event
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
from app.services.booking_state_machine import assert_can_execute_action, BookingAction
from app.models.booking import Booking, BookingStatus
from app.services.platform_payment import sync_booking_platform_fee
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.schemas.booking import (
    BookingAcceptQuote,
    BookingCreate,
    BookingOut,
    BookingQuote,
    BookingReject,
    BookingReopenQuote,
    BookingRequestedRepertoireUpdate,
    BookingUpdate,
    MusicianBookingCreate,
    BookingCalendarSyncRequest,
    CancellationQuoteOut,
)
from app.services.cancellation_policy import (
    CANCELLABLE_STATUSES,
    REFUND_PENDING_APPROVAL,
    RETAINED_STATUSES,
    quote_cancellation,
    record_cancellation,
)
from app.services.availability_match import assert_musician_available
from app.services.booking_contract import create_booking_contract
from app.services.contract_pdf import compute_contract_body_sha256
from app.services.payment_evidence import (
    apply_evidence_urls,
    assert_evidence_uploads,
    normalize_evidence_urls,
)
from app.services.booking_notifications import (
    notify_admins,
    notify_booking_cancelled,
    notify_booking_confirmed,
    notify_booking_created,
    notify_booking_requested_contractor,
    notify_booking_quote_accepted,
    notify_booking_quote_updated,
    notify_booking_quoted,
    notify_booking_rejected,
    notify_booking_updated,
)
from app.services.musician_booking import create_musician_booking
from app.services.uploads import upload_exists


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


@router.get("/cancellation-policy", response_model=CancellationPolicyPublicOut)
def get_public_cancellation_policy():
    """Single source of truth for the platform cancellation policy (FE4-04 / BUG4-06)."""
    return CancellationPolicyPublicOut(
        full_refund_min_days=int(settings.CANCEL_FULL_REFUND_MIN_DAYS),
        partial_refund_min_days=int(settings.CANCEL_PARTIAL_REFUND_MIN_DAYS),
        partial_refund_percent=int(settings.CANCEL_PARTIAL_REFUND_PERCENT),
    )


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

    if payload.event_date < get_lima_today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")
    if (
        payload.event_date == get_lima_today()
        and payload.start_time < datetime.now(ZoneInfo("America/Lima")).time()
    ):
        raise HTTPException(400, "La hora del evento ya pasó")

    assert_musician_available(
        db,
        musician.id,
        payload.event_date,
        payload.start_time,
        end_time=payload.end_time,
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

    from app.services.platform_payment import sync_booking_platform_fee
    sync_booking_platform_fee(db, booking, use_current_settings=True)
    db.add(booking)
    db.commit()
    db.refresh(booking)

    musician_user = musician.user or _load_musician_user(db, musician)
    if musician_user:
        notify_booking_created(db, musician_user, str(booking.id))
        
    contractor_user = current_user
    if contractor_user:
        notify_booking_requested_contractor(db, contractor_user, str(booking.id))
        
    db.commit()

    return booking


@router.post("/musician-created", response_model=BookingOut, status_code=201)
def create_booking_as_musician(
    payload: MusicianBookingCreate,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """El músico crea una propuesta para un cliente; el cliente la acepta, firma y paga."""
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


def _preview_requested_fee_percent(db: Session) -> Decimal:
    from app.models.platform_payment import PlatformPaymentSettings

    row = db.query(PlatformPaymentSettings.platform_fee_percent).first()
    if row and row[0] is not None:
        return Decimal(str(row[0]))
    return Decimal("2")


def _serialize_booking_clean(
    db: Session,
    booking: Booking,
    *,
    viewer_role: str = "owner",
    member_invite_status: str | None = None,
    requested_fee_percent: Decimal | None = None,
) -> BookingOut:
    out = serialize_booking_out(
        booking,
        viewer_role=viewer_role,
        member_invite_status=member_invite_status,
    )
    is_requested = getattr(booking, "status", None) == BookingStatus.requested
    if is_requested or out.platform_fee_percent is None:
        pct = (
            Decimal(str(booking.platform_fee_percent))
            if booking.platform_fee_percent is not None
            else (
                requested_fee_percent
                if requested_fee_percent is not None
                else _preview_requested_fee_percent(db)
            )
        )
        amt = out.platform_fee_amount
        if is_requested or booking.price_agreed is None:
            amt = None
        elif amt is None:
            from app.services.platform_payment import compute_platform_fee_amount

            amt = compute_platform_fee_amount(Decimal(str(booking.price_agreed)), pct)
        out = out.model_copy(update={"platform_fee_percent": pct, "platform_fee_amount": amt})
    return out


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
        fee_pct = (
            _preview_requested_fee_percent(db)
            if any(
                getattr(b, "status", None) == BookingStatus.requested
                or b.platform_fee_percent is None
                for b in bookings
            )
            else None
        )
        return [
            _serialize_booking_clean(
                db, b, viewer_role="owner", requested_fee_percent=fee_pct
            )
            for b in bookings
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

        fee_pct = (
            _preview_requested_fee_percent(db)
            if any(
                getattr(b, "status", None) == BookingStatus.requested
                or b.platform_fee_percent is None
                for b in owned
            )
            else None
        )
        results = [
            _serialize_booking_clean(
                db, b, viewer_role="owner", requested_fee_percent=fee_pct
            )
            for b in owned
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
    return _serialize_booking_clean(
        db,
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

    assert_can_execute_action(booking, BookingAction.UPDATE_REQUEST, current_user)

    update_data = {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if v is not None or k in ("end_time", "location_reference", "event_description")
    }
    if "event_date" in update_data and update_data["event_date"] < get_lima_today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")

    for field, value in update_data.items():
        setattr(booking, field, value)

    if booking.end_time is not None and booking.end_time == booking.start_time:
        raise HTTPException(400, "La hora de fin no puede ser igual a la hora de inicio")
    if (
        any(k in update_data for k in ("event_date", "start_time"))
        and booking.event_date == get_lima_today()
        and booking.start_time < datetime.now(ZoneInfo("America/Lima")).time()
    ):
        raise HTTPException(400, "La hora del evento ya pasó")

    if any(k in update_data for k in ("event_date", "start_time", "end_time")):
        assert_musician_available(
            db,
            booking.musician_id,
            booking.event_date,
            booking.start_time,
            end_time=booking.end_time,
            exclude_booking_id=booking.id,
        )

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

    assert_can_execute_action(booking, BookingAction.REJECT_REQUEST, current_user)

    booking.status = BookingStatus.cancelled
    booking.cancelled_by = "musician"
    booking.rejection_reason = payload.rejection_reason or "Solicitud rechazada por el músico"

    from app.models.booking_link import BookingLink, BookingLinkStatus

    for bl in db.query(BookingLink).filter(BookingLink.booking_id == booking.id).all():
        if bl.status != BookingLinkStatus.completed:
            bl.status = BookingLinkStatus.expired

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

    assert_can_execute_action(booking, BookingAction.QUOTE, current_user)

    assert_musician_available(
        db,
        booking.musician_id,
        booking.event_date,
        booking.start_time,
        end_time=booking.end_time,
        exclude_booking_id=booking.id,
    )

    is_update = booking.status == BookingStatus.accepted

    if not (musician.signature_image_url or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Debes registrar tu firma digital antes de enviar una cotización",
        )

    if not musician.contract_template_body or len(musician.contract_template_body.strip()) < 50:
        from app.services.contract_pdf import (
            DEFAULT_CONTRACT_TEMPLATE_BODY,
            DEFAULT_CONTRACT_TEMPLATE_TITLE,
        )

        if not musician.contract_template_title:
            musician.contract_template_title = DEFAULT_CONTRACT_TEMPLATE_TITLE
        musician.contract_template_body = DEFAULT_CONTRACT_TEMPLATE_BODY

    booking.price_agreed = payload.price_agreed
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

    existing_contract = (
        db.query(Contract).filter(Contract.booking_id == booking.id).first()
    )
    if existing_contract and not existing_contract.contractor_signed:
        db.delete(existing_contract)
        db.flush()

    contractor_user = _load_contractor_user(db, booking.contractor_id)
    musician_user = _load_musician_user(db, musician)
    contractor_profile = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if musician_user and contractor_profile and contractor_user:
        try:
            create_booking_contract(
                db,
                booking,
                musician=musician,
                musician_user=musician_user,
                contractor=contractor_profile,
                contractor_user=contractor_user,
            )
            booking.status = BookingStatus.accepted
        except ValueError:
            booking.status = BookingStatus.accepted
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

    assert_can_execute_action(booking, BookingAction.REJECT_QUOTE, current_user)

    booking.status = BookingStatus.cancelled
    booking.cancelled_by = "contractor"
    booking.rejection_reason = payload.rejection_reason or "Cotización rechazada por el contratista"

    from app.models.booking_link import BookingLink, BookingLinkStatus

    for bl in db.query(BookingLink).filter(BookingLink.booking_id == booking.id).all():
        if bl.status != BookingLinkStatus.completed:
            bl.status = BookingLinkStatus.expired

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
    payload: BookingAcceptQuote | None = None,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    contractor = assert_booking_contractor_owner(db, booking, current_user)

    assert_can_execute_action(booking, BookingAction.ACCEPT_QUOTE, current_user)
    if not is_pre_event(booking):
        raise HTTPException(
            status_code=400,
            detail="No puedes aceptar la cotización porque la fecha u hora del evento ya pasó.",
        )

    assert_musician_available(
        db,
        booking.musician_id,
        booking.event_date,
        booking.start_time,
        end_time=booking.end_time,
        exclude_booking_id=booking.id,
    )

    musician = db.query(MusicianProfile).filter(
        MusicianProfile.id == booking.musician_id
    ).first()
    if not musician:
        raise HTTPException(404, "Músico no encontrado")

    if not (musician.signature_image_url or "").strip():
        raise HTTPException(
            status_code=400,
            detail="El músico aún no ha registrado su firma digital para el contrato",
        )

    musician_user = _load_musician_user(db, musician)
    contractor_user = current_user

    try:
        contract = create_booking_contract(
            db,
            booking,
            musician,
            musician_user,
            contractor,
            contractor_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    expected_hash = compute_contract_body_sha256(contract.title, contract.body)
    if not payload or not payload.contract_body_sha256 or not payload.contract_body_sha256.strip():
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="contract_body_sha256 es obligatorio para aceptar la cotización y el contrato.",
        )
    if payload.contract_body_sha256.strip().lower() != expected_hash.lower():
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="El contenido del contrato cambió desde que lo leíste. Recarga la página antes de aceptar.",
        )
    contract.context = {
        **(contract.context or {}),
        "__signed_body_sha256__": expected_hash,
    }

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

    assert_can_execute_action(booking, BookingAction.REOPEN_QUOTE, current_user)

    data = {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if v is not None or k in ("end_time", "location_reference", "event_description", "requested_repertoire")
    }
    repertoire = data.pop("requested_repertoire", None)
    for field, value in data.items():
        setattr(booking, field, value)

    if booking.event_date < get_lima_today():
        raise HTTPException(400, "La fecha del evento no puede ser anterior a hoy")
    if booking.end_time is not None and booking.end_time == booking.start_time:
        raise HTTPException(400, "La hora de fin no puede ser igual a la hora de inicio")
    if (
        booking.event_date == get_lima_today()
        and booking.start_time < datetime.now(ZoneInfo("America/Lima")).time()
    ):
        raise HTTPException(400, "La hora del evento ya pasó")

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
        end_time=booking.end_time,
        exclude_booking_id=booking.id,
    )

    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if contract:
        db.delete(contract)

    booking.price_agreed = None
    from app.services.platform_payment import sync_booking_platform_fee
    sync_booking_platform_fee(db, booking, use_current_settings=True)
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
    from app.core.limiter import get_client_ip

    return get_client_ip(request) or fallback or "unknown"


def _assert_upload_exists(upload_url: str, label: str) -> None:
    if not upload_exists(upload_url):
        raise HTTPException(
            status_code=400,
            detail=f"No se encontró el archivo de {label}. Vuelve a subirlo.",
        )


@router.get("/{booking_id}/cancellation-quote", response_model=CancellationQuoteOut)
def get_cancellation_quote(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)
    cancelled_by = "musician" if current_user.role == UserRole.musician else "contractor"
    quote = quote_cancellation(db, booking, cancelled_by)
    return CancellationQuoteOut(
        can_cancel=booking.status in CANCELLABLE_STATUSES,
        **quote.__dict__,
    )


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
    payload: BookingReject | None = None,
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    # Bloquear la fila evita dos cancelaciones (y dos reembolsos) simultáneas.
    db.refresh(booking, with_for_update=True)
    if booking.status not in CANCELLABLE_STATUSES:
        raise HTTPException(400, "No se puede cancelar en este estado")
    from app.services.booking_lifecycle import is_event_started
    if booking.status in RETAINED_STATUSES and is_event_started(booking):
        raise HTTPException(
            400,
            "No se puede cancelar una reserva cuyo evento ya inició. Si hubo un inconveniente, registra una queja al finalizar.",
        )

    cancelled_by = "musician" if current_user.role == UserRole.musician else "contractor"
    quote = quote_cancellation(db, booking, cancelled_by)
    record_cancellation(booking, quote)

    if payload and payload.rejection_reason:
        booking.rejection_reason = payload.rejection_reason

    _remove_calendar_event(booking)

    from app.services.booking_share import disable_booking_share
    from app.models.booking_link import BookingLink, BookingLinkStatus

    disable_booking_share(booking)
    for bl in db.query(BookingLink).filter(BookingLink.booking_id == booking.id).all():
        if bl.status != BookingLinkStatus.completed:
            bl.status = BookingLinkStatus.expired

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

    if booking.cancellation_refund_status == REFUND_PENDING_APPROVAL:
        notify_admins(
            db,
            type="admin_refund_approval_requested",
            title="Reembolso por aprobar",
            message=(
                f"Se canceló una reserva con un reembolso de S/ {booking.cancellation_refund_amount:.2f}. "
                "Revísalo y apruébalo en Reservas."
            ),
            meta={"booking_id": str(booking.id)},
        )

    db.commit()
    db.refresh(booking)
    return booking


def _remove_calendar_event(booking: Booking) -> None:
    if not booking.calendar_event_id:
        return
    from sqlalchemy.orm import object_session
    from app.services.calendar_service import cancel_booking_calendar_event

    try:
        cancel_booking_calendar_event(object_session(booking), booking)
    except Exception:  # noqa: BLE001 - no bloquear la cancelación por Calendar
        logging.getLogger(__name__).exception("No se pudo borrar el evento de Calendar %s", booking.calendar_event_id)


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
    else:
        raise HTTPException(400, "No se pudo sincronizar. Asegúrate que el músico líder tenga conectado su Google Calendar y verifica los permisos.")
    
    # Enviar invitaciones (emails y BD) usando la funcion oficial
    try:
        from app.api.v1.endpoints.ensemble_members import create_booking_invites
        from app.schemas.ensemble import BookingMemberInviteCreate
        import uuid
        if str_member_ids:
            invite_payload = BookingMemberInviteCreate(ensemble_member_ids=[uuid.UUID(m) for m in str_member_ids])
            create_booking_invites(booking_id=uuid.UUID(booking_id), payload=invite_payload, db=db, current_user=current_user)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error creando invitaciones de reserva: {e}")

    return serialize_booking_out(
        booking,
        viewer_role=viewer_role,
        member_invite_status=None,
    )






