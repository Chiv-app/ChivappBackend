import os
import sys
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.append(os.getcwd())
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.booking import Booking, BookingStatus, BookingReview
from app.models.email_log import EmailLog
from app.services.email.service import send_templated_email
from sqlalchemy.orm import joinedload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    db = SessionLocal()
    try:
        lima_tz = ZoneInfo("America/Lima")
        now_lima = datetime.now(lima_tz)

        valid_statuses = [
            BookingStatus.in_progress,
            BookingStatus.payment_released,
            BookingStatus.completed,
        ]

        bookings = db.query(Booking).options(
            joinedload(Booking.contractor),
            joinedload(Booking.musician)
        ).filter(Booking.status.in_(valid_statuses)).all()
        
        for b in bookings:
            if not b.event_date or not b.start_time:
                continue
            
            event_dt_unaware = datetime.combine(b.event_date, b.start_time)
            event_dt_lima = event_dt_unaware.replace(tzinfo=lima_tz)
            
            time_since_start = now_lima - event_dt_lima
            if time_since_start < timedelta(hours=5):
                continue
            
            # Use getattr or check if related object is valid before querying user
            contractor_prof = b.contractor
            musician_prof = b.musician
            
            contractor_user = contractor_prof.user if contractor_prof else None
            musician_user = musician_prof.user if musician_prof else None
            
            reviews = db.query(BookingReview).filter(BookingReview.booking_id == b.id).all()
            authors = [r.author_user_id for r in reviews]
            
            if contractor_user and musician_user and (contractor_user.id in authors) and (musician_user.id in authors):
                continue
                
            if contractor_user and contractor_user.id not in authors:
                send_reminder(db, contractor_user, b, musician_user.fullname or "el músico" if musician_user else "el músico", "contractor")
                
            if musician_user and musician_user.id not in authors:
                send_reminder(db, musician_user, b, contractor_user.fullname or "el contratista" if contractor_user else "el contratista", "musician")
                
    finally:
        db.close()

def send_reminder(db, user, booking, other_party_name, role):
    if not user.email or user.email.endswith("@guest.local"):
        return
        
    log = db.query(EmailLog).filter(
        EmailLog.template_slug == "booking_review_reminder",
        EmailLog.recipient == user.email,
        EmailLog.meta.op('->>')('booking_id') == str(booking.id)
    ).first()
    
    if log:
        return
        
    dashboard_path = "/musician/bookings" if role == "musician" else "/contractor/bookings"
    action_url = f"{settings.FRONTEND_URL.rstrip('/')}{dashboard_path}/{booking.id}"
    
    logger.info(f"Enviando recordatorio de reseña para booking {booking.id} a {user.email}")
    send_templated_email(
        db,
        slug="booking_review_reminder",
        to=user.email,
        context={
            "user_name": user.fullname or user.username or "Usuario",
            "other_party_name": other_party_name,
            "action_url": action_url,
        },
        user_id=user.id,
        meta={"booking_id": str(booking.id), "reminder_type": "review"}
    )
    db.commit()

if __name__ == "__main__":
    main()

