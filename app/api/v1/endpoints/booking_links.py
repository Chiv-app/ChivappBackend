import secrets
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.api import deps
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.user import User, UserRole
from app.models.musician_profile import MusicianProfile
from app.models.contractor_profile import ContractorProfile
from app.models.booking import Booking, BookingStatus
from app.schemas.booking_link import (
    BookingLinkCreate,
    BookingLinkOut,
    BookingLinkPublicOut,
    BookingLinkCheckout,
    MusicianLinkProfile
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
        
    musician_profile = db.query(MusicianProfile).filter(MusicianProfile.id == current_user.id).first()
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
        BookingLink.musician_id == current_user.id
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
        
    if link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail=f"Este link de pago ya no está activo ({link.status.value}).")
        
    if link.expires_at < datetime.utcnow():
        link.status = BookingLinkStatus.expired
        db.commit()
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")
        
    return BookingLinkPublicOut(
        token=link.token,
        price=float(link.price),
        event_date=link.event_date,
        start_time=link.start_time,
        musician=MusicianLinkProfile(
            id=link.musician.id,
            stage_name=link.musician.stage_name,
            profile_picture_url=link.musician.user.profile_picture_url
        ),
        status=link.status.value,
        expires_at=link.expires_at
    )

@router.post("/{token}/checkout")
def checkout_booking_link(
    token: str,
    payload: BookingLinkCheckout,
    db: Session = Depends(deps.get_db)
):
    """
    Client submits the accordion form (Event + Info).
    This creates a real Booking and initiates payment.
    """
    link = db.query(BookingLink).filter(BookingLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link de pago no encontrado.")
        
    if link.status != BookingLinkStatus.active:
        raise HTTPException(status_code=400, detail="Este link de pago ya no está activo.")
        
    if link.expires_at < datetime.utcnow():
        link.status = BookingLinkStatus.expired
        db.commit()
        raise HTTPException(status_code=400, detail="Este link de pago ha expirado.")

    # 1. Find or create the user (Contractor)
    # The requirement: "silently registers guest"
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    
    if not user:
        from app.services.auth import generate_verification_token
        from app.core.security import get_password_hash
        import uuid
        
        user = User(
            email=email,
            fullname=payload.fullname,
            phone=payload.phone,
            role=UserRole.contractor,
            is_active=True,
            is_verified=False, # Unverified until they confirm email or login OTP
            password_hash=get_password_hash(secrets.token_urlsafe(32)), # Random password
            email_verification_token=generate_verification_token(email),
            email_verification_expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        db.add(user)
        db.flush()
        
        contractor = ContractorProfile(
            id=user.id,
            fullname=payload.fullname,
            document_number=payload.document_number,
        )
        db.add(contractor)
        db.flush()
    else:
        # Existing user. Ensure contractor profile exists.
        contractor = db.query(ContractorProfile).filter(ContractorProfile.id == user.id).first()
        if not contractor:
            contractor = ContractorProfile(id=user.id, fullname=payload.fullname)
            db.add(contractor)
            db.flush()
            
    # 2. Create the Booking
    booking = Booking(
        musician_id=link.musician_id,
        contractor_id=user.id,
        event_date=payload.event_date,
        start_time=payload.start_time,
        event_type=payload.event_type,
        location_address=payload.location_address,
        location_city=payload.location_city,
        agreed_price=link.price,
        status=BookingStatus.pending_payment,
    )
    db.add(booking)
    db.flush()
    
    # 3. Mark link as completed
    link.status = BookingLinkStatus.completed
    link.booking_id = booking.id
    
    db.commit()
    
    # TODO: In a real flow, this would generate MercadoPago preference 
    # and return an init_point. For now, we return the booking ID.
    return {"message": "Reserva creada exitosamente", "booking_id": booking.id}

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
        
    if link.musician_id != current_user.id:
        raise HTTPException(status_code=403, detail="No autorizado.")
        
    if link.status == BookingLinkStatus.completed:
        raise HTTPException(status_code=400, detail="El link ya fue completado.")
        
    link.status = BookingLinkStatus.active
    link.expires_at = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    db.refresh(link)
    
    return link
