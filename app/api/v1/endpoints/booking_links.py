import secrets
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from app.api.v1.endpoints.auth import _issue_login
from app.services.booking_contract import create_booking_contract
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.api import deps
from app.core.config import settings
from app.core.limiter import limiter
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.user import User, UserRole
from app.models.musician_profile import MusicianProfile
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
    BookingLinkOtpRequest
)

router = APIRouter()

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
    if not musician_profile:
        raise HTTPException(status_code=400, detail="Perfil de músico no encontrado.")
        
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
    ).order_with_labels().order_by(BookingLink.created_at.desc()).all()
    
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
        
    if link.expires_at and link.expires_at < datetime.utcnow():
        if link.status == BookingLinkStatus.active:
            link.status = BookingLinkStatus.expired
            db.commit()
            db.refresh(link)

    # Si el link figuraba como "completed" pero la reserva aún no ha sido pagada,
    # permitir que el cliente continúe el proceso de pago.
    if link.status == BookingLinkStatus.completed and link.booking_id:
        existing_booking = db.get(Booking, link.booking_id)
        if existing_booking and existing_booking.status in (
            BookingStatus.accepted,
            BookingStatus.contract_pending,
            BookingStatus.contract_signed,
            BookingStatus.payment_pending,
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

    return BookingLinkPublicOut(
        token=link.token,
        price=float(link.price),
        fee_percent=float(settings_obj.platform_fee_percent),
        fee_amount=float(fee_amount),
        total_payable=float(total_payable),
        event_date=link.event_date,
        start_time=link.start_time,
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
            location_city=link.musician.location_city,
            reviews=reviews_data
        ),
        status=link.status.value,
        expires_at=link.expires_at
    )


@router.post("/{token}/check-identity", response_model=BookingLinkIdentityResponse)
def check_identity(
    token: str,
    payload: BookingLinkIdentityCheck,
    db: Session = Depends(deps.get_db)
):
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link no encontrado.")

    email = payload.email.lower().strip()
    
    # 1. Self check
    musician_user = link.musician.user
    if musician_user.email == email:
        return BookingLinkIdentityResponse(status="self", message="No puedes usar tu propia cuenta de músico para contratarte a ti mismo.")
        
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return BookingLinkIdentityResponse(status="new")
        
    if user.is_verified:
        return BookingLinkIdentityResponse(status="exists_verified", message="Este correo ya existe y está verificado.")
    else:
        if user.email_verification_token:
            return BookingLinkIdentityResponse(status="new")
        if link.booking_id:
            existing_b = db.get(Booking, link.booking_id)
            contractor_p = db.query(ContractorProfile).filter(ContractorProfile.user_id == user.id).first()
            if existing_b and contractor_p and existing_b.contractor_id == contractor_p.id:
                return BookingLinkIdentityResponse(status="new")
        return BookingLinkIdentityResponse(status="exists_unverified", message="Este correo ya existe pero requiere contraseña.")

@router.post("/{token}/send-otp")
@limiter.limit(settings.RATE_LIMIT_LOGIN)
def send_identity_otp(
    token: str,
    payload: BookingLinkOtpRequest,
    request: Request,
    db: Session = Depends(deps.get_db)
):
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link no encontrado.")
        
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    
    if not user or not user.is_verified:
        raise HTTPException(status_code=400, detail="Usuario no válido para OTP.")
        
    from app.services.email.service import send_templated_email
    from app.services.email.defaults import APP_NAME
    import hashlib
    
    otp = str(secrets.randbelow(1000000)).zfill(6)
    user.login_otp_hash = hashlib.sha256(f"{user.id}:{otp}".encode()).hexdigest()
    user.login_otp_expires_at = datetime.utcnow() + timedelta(minutes=15)
    db.commit()
    
    try:
        send_templated_email(
            to_email=user.email,
            subject=f"Tu código de verificación para {APP_NAME}",
            template_name="login_otp",
            context={
                "fullname": user.fullname or "Usuario",
                "otp": otp,
                "app_name": APP_NAME
            }
        )
    except Exception as e:
        print("Template login_otp failed to send", e)
        pass
        
    return {"message": "OTP enviado"}


@router.post("/{token}/checkout")
def checkout_booking_link(
    token: str,
    payload: BookingLinkCheckout,
    response: Response,
    db: Session = Depends(deps.get_db)
):
    """
    Client submits the accordion form (Event + Info).
    This creates a real Booking and initiates payment.
    """
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link de pago no encontrado.")
        
    if link.expires_at and link.expires_at < datetime.utcnow():
        if link.status == BookingLinkStatus.active:
            link.status = BookingLinkStatus.expired
            db.commit()
            db.refresh(link)

    existing_booking = db.get(Booking, link.booking_id) if link.booking_id else None
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
        

    # 1. Find or create the user (Contractor)
    # The requirement: "silently registers guest"
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    
    if not user:
        if payload.phone:
            existing_phone = db.query(User).filter(User.phone == payload.phone).first()
            if existing_phone:
                raise HTTPException(status_code=400, detail="Este celular ya está registrado a otra cuenta.")
        
        from app.core.hashing import hash_password
        
        user = User(
            email=email,
            fullname=payload.fullname,
            phone=payload.phone,
            role=UserRole.contractor,
            is_active=True,
            is_verified=False, # Unverified until they confirm email or login OTP
            password_hash=hash_password(secrets.token_urlsafe(32)), # Random password
            email_verification_token=secrets.token_urlsafe(32),
            email_verification_expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        db.add(user)
        db.flush()
        
        contractor = ContractorProfile(
            user_id=user.id,
            document_number=payload.document_number,
        )
        db.add(contractor)
        db.flush()
    else:
        # Existing user. Validate credentials based on their status.
        from app.core.hashing import verify_password
        contractor_check = db.query(ContractorProfile).filter(ContractorProfile.user_id == user.id).first()
        owns_existing_booking = bool(
            existing_booking and contractor_check and existing_booking.contractor_id == contractor_check.id
        )
        
        if user.is_verified:
            # Verified user requires OTP
            if not payload.otp and not owns_existing_booking:
                raise HTTPException(status_code=400, detail="Este correo está verificado y requiere un código OTP.")
            if payload.otp:
                if not user.login_otp_hash or not user.login_otp_expires_at:
                    raise HTTPException(status_code=400, detail="El código ha expirado o no ha sido solicitado. Solicítelo nuevamente.")
                if user.login_otp_expires_at < datetime.utcnow():
                    raise HTTPException(status_code=400, detail="El código OTP ha expirado. Solicite uno nuevo.")
                
                import hashlib
                expected_hash = hashlib.sha256(f"{user.id}:{payload.otp}".encode()).hexdigest()
                if user.login_otp_hash != expected_hash:
                    raise HTTPException(status_code=400, detail="El código OTP es incorrecto.")
                
                # OTP validated successfully, clear it
                user.login_otp_hash = None
                user.login_otp_expires_at = None
        else:
            # Unverified user: allow guest accounts (with email_verification_token) or retry on same booking
            is_guest_account = bool(user.email_verification_token) or owns_existing_booking
            if not is_guest_account:
                if not payload.password:
                    raise HTTPException(status_code=400, detail="Este correo ya está en uso. Ingrese su contraseña.")
                if not verify_password(payload.password, user.password_hash):
                    raise HTTPException(status_code=400, detail="Contraseña incorrecta.")
        
        # Ensure contractor profile exists.
        contractor = contractor_check
        if not contractor:
            contractor = ContractorProfile(user_id=user.id, document_number=payload.document_number)
            db.add(contractor)
            db.flush()
        elif payload.document_number and not contractor.document_number:
            contractor.document_number = payload.document_number
            db.flush()
            
        if payload.phone and user.phone != payload.phone:
            existing_phone = db.query(User).filter(User.phone == payload.phone).first()
            if existing_phone and existing_phone.id != user.id:
                raise HTTPException(status_code=400, detail="Este celular ya está registrado a otra cuenta.")
            user.phone = payload.phone
            db.flush()

    from app.services.platform_payment import sync_booking_platform_fee
    from app.services.uploads import save_data_url_upload

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
    
    # 2. Reuse existing unpaid Booking or create a new Booking
    if existing_booking and booking_is_unpaid and existing_booking.contractor_id == contractor.id:
        booking = existing_booking
        booking.event_date = payload.event_date
        booking.start_time = payload.start_time
        booking.event_type = payload.event_type
        booking.location_address = payload.location_address
        booking.location_city = payload.location_city
        booking.price_agreed = link.price
        booking.status = BookingStatus.contract_signed if saved_sig_url else BookingStatus.contract_pending
    else:
        booking = Booking(
            musician_id=link.musician_id,
            contractor_id=contractor.id,
            event_date=payload.event_date,
            start_time=payload.start_time,
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
    try:
        contract = create_booking_contract(
            db, 
            booking, 
            musician=link.musician, 
            musician_user=link.musician.user, 
            contractor=contractor, 
            contractor_user=user
        )
        # Mark terms as implicitly accepted via the public checkout form checkbox
        contract.terms_accepted = True
        contract.terms_accepted_at = datetime.utcnow()
        contract.terms_accepted_ip = "public_checkout"
        
        if saved_sig_url:
            contract.contractor_signature_url = saved_sig_url
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = datetime.utcnow()
            contract.contractor_sign_ip = "public_checkout"
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
