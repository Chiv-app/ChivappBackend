import secrets
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from app.api.v1.endpoints.auth import (
    _issue_login,
    generate_and_send_login_otp,
    verify_login_otp_or_raise,
)
from app.services.availability_match import assert_musician_available
from app.services.booking_contract import create_booking_contract
from app.services.email.auth_emails import send_email_verification
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.api import deps
from app.core.config import settings
from app.core.limiter import get_client_ip, limiter
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.user import User, UserRole
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.contractor_profile import ContractorProfile
from app.models.booking import Booking, BookingStatus, BookingReview
from app.models.musician_media import MusicianMedia, MediaType
from app.services.platform_payment import get_or_create_platform_payment_settings, compute_platform_fee_amount, compute_contractor_payable_total
from app.schemas.booking_link import (
    BookingLinkCreate,
    BookingLinkOut,
    BookingLinkPublicOut,
    BookingLinkCheckout,
    MusicianLinkProfile,
    BookingLinkIdentityResponse,
    BookingLinkIdentityCheck,
    BookingLinkOtpRequest,
    CancellationPolicyPublicOut,
)

router = APIRouter()


def _get_lima_today() -> date:
    return datetime.now(ZoneInfo("America/Lima")).date()


def _assert_musician_eligible_for_link(musician: MusicianProfile | None, *, for_owner: bool = False) -> None:
    if not musician or not musician.user:
        raise HTTPException(
            status_code=400 if for_owner else 404,
            detail="Perfil de músico no encontrado.",
        )
    if not musician.user.is_active or not musician.user.is_verified:
        raise HTTPException(
            status_code=403 if for_owner else 404,
            detail=(
                "Tu cuenta de músico debe estar activa y verificada para usar links de pago."
                if for_owner
                else "El músico no está disponible para reservas."
            ),
        )
    if musician.status != ProfileStatus.published:
        raise HTTPException(
            status_code=403 if for_owner else 404,
            detail=(
                "Tu perfil de músico debe estar publicado para crear links de pago."
                if for_owner
                else "El músico no está disponible para reservas."
            ),
        )
    if not (musician.signature_image_url and musician.signature_image_url.strip()):
        raise HTTPException(
            status_code=400 if for_owner else 404,
            detail=(
                "Debes registrar tu firma digital en tu perfil antes de crear links de pago."
                if for_owner
                else "El músico no está disponible para reservas."
            ),
        )


@router.post("", response_model=BookingLinkOut, include_in_schema=False)
@router.post("/", response_model=BookingLinkOut)
def create_booking_link(
    payload: BookingLinkCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user)
):
    """
    Musician creates a smart payment link (contrata rápida).
    """
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Sólo los músicos pueden crear links de pago.")
        
    musician_profile = db.query(MusicianProfile).filter(MusicianProfile.user_id == current_user.id).first()
    _assert_musician_eligible_for_link(musician_profile, for_owner=True)

    lima_now = datetime.now(ZoneInfo("America/Lima"))
    if payload.event_date:
        if payload.event_date < lima_now.date():
            raise HTTPException(status_code=400, detail="La fecha del evento no puede ser anterior a hoy.")
        if payload.event_date == lima_now.date() and payload.start_time and payload.start_time < lima_now.time():
            raise HTTPException(status_code=400, detail="La hora del evento ya pasó.")
        
    token = secrets.token_urlsafe(16)
    
    booking_link = BookingLink(
        token=token,
        musician_id=musician_profile.id,
        price=payload.price,
        event_date=payload.event_date,
        start_time=payload.start_time,
        client_email=payload.client_email,
        expires_at=datetime.utcnow() + timedelta(hours=24)
    )
    
    db.add(booking_link)
    db.commit()
    db.refresh(booking_link)
    
    return booking_link

@router.get("/me", response_model=list[BookingLinkOut])
def get_my_booking_links(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user)
):
    """
    List all smart payment links created by the current musician.
    """
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Sólo los músicos pueden ver sus links de pago.")
        
    links = db.query(BookingLink).filter(
        BookingLink.musician.has(user_id=current_user.id)
    ).order_by(BookingLink.created_at.desc()).all()
    
    return links

@router.get("/{token}", response_model=BookingLinkPublicOut)
def get_booking_link(
    token: str,
    db: Session = Depends(deps.get_db)
):
    """
    Public endpoint for a client to load the payment link details.
    """
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link de pago no encontrado.")

    _assert_musician_eligible_for_link(link.musician, for_owner=False)
        
    if link.expires_at and link.expires_at < datetime.utcnow():
        if link.status == BookingLinkStatus.active:
            link.status = BookingLinkStatus.expired
            db.commit()
            db.refresh(link)

    # Si el link tiene una reserva vinculada que fue cancelada, expirar el link.
    if link.booking_id:
        existing_booking = db.get(Booking, link.booking_id)
        if existing_booking and existing_booking.status == BookingStatus.cancelled:
            if link.status != BookingLinkStatus.expired:
                link.status = BookingLinkStatus.expired
                db.commit()
                db.refresh(link)
        elif (
            link.status == BookingLinkStatus.completed
            and existing_booking
            and existing_booking.status
            in (
                BookingStatus.accepted,
                BookingStatus.contract_pending,
                BookingStatus.contract_signed,
                BookingStatus.payment_pending,
            )
        ):
            if not link.expires_at or link.expires_at >= datetime.utcnow():
                link.status = BookingLinkStatus.active
                db.commit()
                db.refresh(link)
            
    if link.status == BookingLinkStatus.expired:
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")
    elif link.status == BookingLinkStatus.completed:
        raise HTTPException(status_code=400, detail="Este link de pago ya ha sido completado.")
    elif link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail=f"Este link de pago ya no está activo ({link.status.value}).")
        
    # Extraer multimedia del músico (fotos y videos) desde MusicianMedia
    media_records = (
        db.query(MusicianMedia)
        .filter(MusicianMedia.musician_id == link.musician_id)
        .order_by(MusicianMedia.order_index.asc(), MusicianMedia.created_at.asc())
        .all()
    )
    
    gallery_images = [m.url for m in media_records if m.type == MediaType.image and m.url]
    videos = [m.url for m in media_records if m.type == MediaType.video and m.url]
    
    # Fallback to legacy fields if empty
    if not gallery_images and getattr(link.musician, "gallery_images", None):
        gallery_images = list(link.musician.gallery_images)
    
    # Ensure profile photo is first if it exists
    if link.musician.profile_image_url and link.musician.profile_image_url not in gallery_images:
        gallery_images = [link.musician.profile_image_url] + gallery_images
    elif link.musician.profile_image_url and gallery_images and gallery_images[0] != link.musician.profile_image_url:
        gallery_images = [link.musician.profile_image_url] + [u for u in gallery_images if u != link.musician.profile_image_url]

    showreel_video_url = getattr(link.musician, "showreel_video_url", None)
    
    # Merge MusicianMedia videos with legacy videos and showreel
    raw_legacy_videos = list(getattr(link.musician, "videos", [])) if getattr(link.musician, "videos", None) else []
    legacy_videos = []
    for v in raw_legacy_videos:
        if v:
            for sub_v in v.split(","):
                if sub_v.strip():
                    legacy_videos.append(sub_v.strip())
    all_videos = videos + legacy_videos
    if showreel_video_url:
        all_videos.insert(0, showreel_video_url)
        
    # Deduplicate preserving order
    videos = []
    seen_videos = set()
    for v in all_videos:
        if v and v not in seen_videos:
            videos.append(v)
            seen_videos.add(v)
        
    # Extraer reseñas completadas del músico
    reviews_query = (
        db.query(BookingReview)
        .join(Booking)
        .filter(Booking.musician_id == link.musician_id)
        .filter(BookingReview.comment.isnot(None))
        .order_by(BookingReview.created_at.desc())
        .limit(10)
        .all()
    )
    
    reviews_data = [
        {
            "guest_name": r.guest_name or (r.author.fullname if r.author else "Usuario anónimo"),
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at
        }
        for r in reviews_query
    ]
        
    # Calculate platform fee for public display
    settings_obj = get_or_create_platform_payment_settings(db)
    fee_amount = compute_platform_fee_amount(link.price, settings_obj.platform_fee_percent)
    total_payable = compute_contractor_payable_total(link.price, settings_obj.platform_fee_percent)

    from app.services.contract_pdf import (
        DEFAULT_CONTRACT_TEMPLATE_BODY,
        DEFAULT_CONTRACT_TEMPLATE_TITLE,
    )
    from app.services.html_sanitizer import sanitize_contract_html

    raw_title = (link.musician.contract_template_title or "").strip() or DEFAULT_CONTRACT_TEMPLATE_TITLE
    raw_body = (link.musician.contract_template_body or "").strip() or DEFAULT_CONTRACT_TEMPLATE_BODY

    return BookingLinkPublicOut(
        token=link.token,
        price=float(link.price),
        fee_percent=float(settings_obj.platform_fee_percent),
        fee_amount=float(fee_amount),
        total_payable=float(total_payable),
        event_date=link.event_date,
        start_time=link.start_time,
        client_email=link.client_email,
        musician=MusicianLinkProfile(
            id=link.musician.id,
            stage_name=link.musician.stage_name,
            profile_image_url=link.musician.profile_image_url,
            portfolio_description=link.musician.portfolio_description or link.musician.bio,
            gallery_images=gallery_images,
            videos=videos,
            showreel_video_url=showreel_video_url,
            rating_avg=float(link.musician.rating_avg) if link.musician.rating_avg else None,
            rating_count=link.musician.rating_count,
            genres=link.musician.genres,
            instruments=link.musician.instruments,
            location_city=link.musician.location_city,
            reviews=reviews_data
        ),
        status=link.status.value,
        expires_at=link.expires_at,
        cancellation_policy=CancellationPolicyPublicOut(
            full_refund_min_days=int(settings.CANCEL_FULL_REFUND_MIN_DAYS),
            partial_refund_min_days=int(settings.CANCEL_PARTIAL_REFUND_MIN_DAYS),
            partial_refund_percent=int(settings.CANCEL_PARTIAL_REFUND_PERCENT),
        ),
        contract_template_title=raw_title,
        contract_template_body=sanitize_contract_html(raw_body),
    )


@router.post("/{token}/check-identity", response_model=BookingLinkIdentityResponse)
@limiter.limit(settings.RATE_LIMIT_LOGIN)
def check_identity(
    token: str,
    payload: BookingLinkIdentityCheck,
    request: Request,
    db: Session = Depends(deps.get_db),
    session_user: User | None = Depends(deps.get_current_user_optional),
):
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link no encontrado.")
    _assert_musician_eligible_for_link(link.musician, for_owner=False)
    if link.expires_at and link.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")
    if link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail="Este link de pago ya no está activo.")

    email = payload.email.lower().strip()
    
    # 1. Self check
    musician_user = link.musician.user
    if musician_user.email.lower() == email:
        return BookingLinkIdentityResponse(status="self", message="No puedes usar tu propia cuenta de músico para contratarte a ti mismo.")
        
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if not user:
        return BookingLinkIdentityResponse(status="new")

    if session_user and session_user.id == user.id:
        return BookingLinkIdentityResponse(status="new")

    if user.is_verified or not user.password_hash:
        return BookingLinkIdentityResponse(
            status="exists_verified",
            message="Este correo ya existe. Te enviaremos un código de verificación.",
        )

    return BookingLinkIdentityResponse(
        status="exists_unverified",
        message="Este correo ya existe pero requiere contraseña.",
    )


@router.post("/{token}/send-otp")
@limiter.limit(settings.RATE_LIMIT_OTP)
def send_identity_otp(
    token: str,
    payload: BookingLinkOtpRequest,
    request: Request,
    db: Session = Depends(deps.get_db)
):
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link no encontrado.")
    _assert_musician_eligible_for_link(link.musician, for_owner=False)
    if link.expires_at and link.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")
    if link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail="Este link de pago ya no está activo.")
        
    email = payload.email.lower().strip()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    
    if user and user.is_active and user.role != UserRole.admin:
        generate_and_send_login_otp(db, user)
    return {"message": "OTP enviado"}


@router.post("/{token}/checkout")
@limiter.limit(settings.RATE_LIMIT_LOGIN)
def checkout_booking_link(
    token: str,
    payload: BookingLinkCheckout,
    request: Request,
    response: Response,
    db: Session = Depends(deps.get_db),
    session_user: User | None = Depends(deps.get_current_user_optional),
):
    """
    Client submits the accordion form (Event + Info).
    This creates a real Booking and initiates payment.
    """
    link = (
        db.query(BookingLink)
        .filter(BookingLink.token == token)
        .with_for_update()
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link de pago no encontrado.")

    _assert_musician_eligible_for_link(link.musician, for_owner=False)
        
    if link.expires_at and link.expires_at < datetime.utcnow():
        if link.status == BookingLinkStatus.active:
            link.status = BookingLinkStatus.expired
            db.commit()
            db.refresh(link)

    existing_booking = db.get(Booking, link.booking_id) if link.booking_id else None
    if existing_booking and existing_booking.status == BookingStatus.cancelled:
        if link.status != BookingLinkStatus.expired:
            link.status = BookingLinkStatus.expired
            db.commit()
            db.refresh(link)
    booking_is_unpaid = bool(
        existing_booking
        and existing_booking.status
        in (
            BookingStatus.accepted,
            BookingStatus.contract_pending,
            BookingStatus.contract_signed,
            BookingStatus.payment_pending,
        )
    )
    if link.status == BookingLinkStatus.completed and booking_is_unpaid:
        if not link.expires_at or link.expires_at >= datetime.utcnow():
            link.status = BookingLinkStatus.active
            db.flush()
            
    if link.status == BookingLinkStatus.expired:
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")
    elif link.status == BookingLinkStatus.completed:
        raise HTTPException(status_code=400, detail="Este link de pago ya ha sido completado.")
    elif link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail="Este link de pago ya no está activo.")

    email = payload.email.lower().strip()
    if link.client_email and link.client_email.strip().lower() != email:
        raise HTTPException(
            status_code=403,
            detail="Este link de pago fue emitido exclusivamente para otro correo electrónico.",
        )
    if link.event_date and payload.event_date != link.event_date:
        raise HTTPException(
            status_code=400,
            detail="La fecha del evento no coincide con la fijada en el link de pago.",
        )
    if link.start_time and payload.start_time != link.start_time:
        raise HTTPException(
            status_code=400,
            detail="La hora de inicio no coincide con la fijada en el link de pago.",
        )

    effective_date = link.event_date or payload.event_date
    effective_start = link.start_time or payload.start_time

    lima_now = datetime.now(ZoneInfo("America/Lima"))
    if effective_date < lima_now.date():
        raise HTTPException(status_code=400, detail="La fecha del evento no puede ser anterior a hoy.")
    if effective_date == lima_now.date() and effective_start < lima_now.time():
        raise HTTPException(status_code=400, detail="La hora del evento ya pasó.")

    # 1. Find or create the user (Contractor)
    if link.musician.user.email.lower() == email:
        raise HTTPException(
            status_code=400,
            detail="No puedes usar tu propia cuenta de músico para contratarte a ti mismo.",
        )

    from app.services.uniqueness import (
        assert_phone_unique,
        normalize_document_number,
        normalize_phone,
    )

    norm_doc = normalize_document_number(payload.document_number)
    norm_phone = normalize_phone(payload.phone)
    user = db.query(User).filter(func.lower(User.email) == email).first()

    if not user:
        if norm_phone:
            try:
                assert_phone_unique(db, norm_phone)
            except HTTPException:
                raise HTTPException(status_code=400, detail="Este celular ya está registrado a otra cuenta.")
        if norm_doc:
            existing_doc = (
                db.query(ContractorProfile)
                .filter(
                    func.upper(func.replace(func.btrim(ContractorProfile.document_number), " ", ""))
                    == norm_doc
                )
                .first()
            )
            if existing_doc:
                raise HTTPException(
                    status_code=409,
                    detail="Este número de documento ya está registrado en otra cuenta.",
                )

        user = User(
            email=email,
            fullname=payload.fullname,
            phone=norm_phone,
            role=UserRole.contractor,
            is_active=True,
            is_verified=False,
            password_hash=None,
        )
        db.add(user)
        db.flush()
        try:
            send_email_verification(db, user)
        except Exception:
            pass

        # SEC4-08: una cuenta recién creada sin verificar correo no bloquea el DNI
        # en el índice único de contractor_profile (se guarda en el snapshot del contrato).
        contractor = ContractorProfile(
            user_id=user.id,
            document_number=None,
        )
        db.add(contractor)
        db.flush()
    else:
        from app.core.hashing import verify_password

        if getattr(user, "is_active", True) is False:
            raise HTTPException(status_code=403, detail="Tu cuenta está desactivada.")
        if user.role != UserRole.contractor:
            raise HTTPException(
                status_code=403,
                detail="Este correo pertenece a una cuenta que no es de contratista.",
            )

        is_authenticated_same_user = bool(session_user and session_user.id == user.id)
        if not is_authenticated_same_user:
            if payload.otp:
                verify_login_otp_or_raise(db, user, payload.otp)
            elif payload.password and user.password_hash:
                if not verify_password(payload.password, user.password_hash):
                    raise HTTPException(status_code=401, detail="Contraseña incorrecta.")
            else:
                if user.is_verified or not user.password_hash:
                    raise HTTPException(
                        status_code=401,
                        detail="Este correo ya está registrado y requiere un código OTP.",
                    )
                raise HTTPException(
                    status_code=401,
                    detail="Este correo ya está en uso. Ingrese su contraseña.",
                )

        if norm_doc:
            existing_doc = (
                db.query(ContractorProfile)
                .filter(
                    func.upper(func.replace(func.btrim(ContractorProfile.document_number), " ", ""))
                    == norm_doc,
                    ContractorProfile.user_id != user.id,
                )
                .first()
            )
            if existing_doc:
                raise HTTPException(
                    status_code=409,
                    detail="Este número de documento ya está registrado en otra cuenta.",
                )

        user_is_verified = bool(user.is_verified or user.email_verified_at is not None)
        contractor_check = db.query(ContractorProfile).filter(ContractorProfile.user_id == user.id).first()
        contractor = contractor_check
        if not contractor:
            contractor = ContractorProfile(
                user_id=user.id,
                document_number=norm_doc if user_is_verified else None,
            )
            db.add(contractor)
            db.flush()
        elif norm_doc and not contractor.document_number and user_is_verified:
            contractor.document_number = norm_doc
            db.flush()

        if norm_phone and not user.phone:
            try:
                assert_phone_unique(db, norm_phone, exclude_user_id=user.id)
            except HTTPException:
                raise HTTPException(status_code=400, detail="Este celular ya está registrado a otra cuenta.")
            user.phone = norm_phone
            db.flush()

    reuse_existing = False
    if existing_booking and booking_is_unpaid:
        if existing_booking.contractor_id != contractor.id:
            raise HTTPException(
                status_code=409,
                detail="Este link de pago ya está siendo procesado por otro cliente.",
            )
        reuse_existing = True

    assert_musician_available(
        db,
        link.musician_id,
        effective_date,
        effective_start,
        exclude_booking_id=existing_booking.id if reuse_existing else None,
    )

    from app.services.platform_payment import sync_booking_platform_fee
    from app.services.uploads import assert_private_upload_owned, save_data_url_upload

    saved_sig_url = payload.signature_image_url
    if saved_sig_url and saved_sig_url.startswith("data:image/"):
        try:
            saved_sig_url = save_data_url_upload(
                saved_sig_url,
                private=True,
                owner_id=user.id,
                db=db,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif saved_sig_url:
        assert_private_upload_owned(db, saved_sig_url, user.id, "firma")
    
    # 2. Reuse existing unpaid Booking or create a new Booking
    if reuse_existing:
        booking = existing_booking
        booking.event_date = effective_date
        booking.start_time = effective_start
        booking.event_type = payload.event_type
        booking.location_address = payload.location_address
        booking.location_city = payload.location_city
        booking.price_agreed = link.price
        booking.status = BookingStatus.contract_signed if saved_sig_url else BookingStatus.contract_pending
    else:
        booking = Booking(
            musician_id=link.musician_id,
            contractor_id=contractor.id,
            event_date=effective_date,
            start_time=effective_start,
            event_type=payload.event_type,
            location_address=payload.location_address,
            location_city=payload.location_city,
            price_agreed=link.price,
            status=BookingStatus.contract_signed if saved_sig_url else BookingStatus.contract_pending,
        )
        db.add(booking)
    sync_booking_platform_fee(db, booking, use_current_settings=True)
    db.flush()
    
    # 3. Link the booking to the payment link (keep link active until payment is approved)
    link.booking_id = booking.id
    
    # Generate contract
    db.flush()
    keep_doc_in_profile = bool(
        contractor.document_number
        and (user.is_verified or user.email_verified_at is not None)
    )
    try:
        with db.no_autoflush:
            if not keep_doc_in_profile and norm_doc:
                contractor.document_number = norm_doc
            contract = create_booking_contract(
                db, 
                booking, 
                musician=link.musician, 
                musician_user=link.musician.user, 
                contractor=contractor, 
                contractor_user=user,
                refresh_snapshot=True,
            )
            if not keep_doc_in_profile:
                contractor.document_number = None
        # Mark terms as implicitly accepted via the public checkout form checkbox
        from app.services.contract_pdf import compute_contract_body_sha256

        client_ip = get_client_ip(request) or "public_checkout"
        contract.terms_accepted = True
        contract.terms_accepted_at = datetime.utcnow()
        contract.terms_accepted_ip = client_ip
        ctx = dict(contract.context or {})
        ctx["__signed_body_sha256__"] = compute_contract_body_sha256(
            contract.title, contract.body
        )
        contract.context = ctx

        if saved_sig_url:
            contract.contractor_signature_url = saved_sig_url
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = datetime.utcnow()
            contract.contractor_sign_ip = client_ip
            booking.status = BookingStatus.contract_signed
    except ValueError as e:
        db.rollback()
        error_msg = str(e)
        if "Configura tu firma digital" in error_msg:
            error_msg = "El músico aún no ha configurado su firma digital obligatoria. Por favor contáctalo para que pueda recibir reservas."
        raise HTTPException(status_code=400, detail=error_msg)
    
    db.commit()
    
    # Authenticate the user implicitly so they can view the dashboard and pay
    _issue_login(response, user)
    
    return {
        "message": "Reserva creada exitosamente",
        "booking_id": booking.id,
        "signature_image_url": saved_sig_url,
        "contract_body_sha256": (contract.context or {}).get("__signed_body_sha256__"),
    }

@router.post("/{token}/reset-expiration", response_model=BookingLinkOut)
def reset_link_expiration(
    token: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user)
):
    """
    Musician can reset expiration time to 24h from now.
    """
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link de pago no encontrado.")
        
    if link.musician.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No autorizado.")
        
    if link.status == BookingLinkStatus.completed:
        raise HTTPException(status_code=400, detail="El link ya fue completado.")
        
    link.status = BookingLinkStatus.active
    link.expires_at = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    db.refresh(link)
    
    return link

# Trigger deployment
