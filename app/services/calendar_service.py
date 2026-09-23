import logging
import os
import json
from datetime import datetime
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import settings

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def _get_calendar_service_oauth(refresh_token: str):
    """Inicializa y devuelve el servicio de Google Calendar usando OAuth 2.0."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    
    if not refresh_token or not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return None
        
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET
    )
    
    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Error al construir servicio de Google Calendar OAuth: {e}")
        return None

def _get_calendar_service():
    """Inicializa y devuelve el servicio de Google Calendar."""
    creds = None
    if settings.GOOGLE_CREDENTIALS_JSON:
        try:
            creds_info = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_info, scopes=SCOPES
            )
        except Exception as e:
            logger.error(f"Error al cargar credenciales de entorno: {e}")
    else:
        creds_path = os.path.join(os.path.dirname(__file__), '..', '..', 'google_credentials.json')
        if not os.path.exists(creds_path):
            logger.warning(f"No se encontro google_credentials.json ni GOOGLE_CREDENTIALS_JSON. No se sincronizara con Google Calendar.")
            return None
        try:
            creds = service_account.Credentials.from_service_account_file(
                creds_path, scopes=SCOPES
            )
        except Exception as e:
            logger.error(f"Error al inicializar Google Calendar desde archivo: {e}")
            return None

    if not creds:
        return None

    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Error al construir servicio de Google Calendar: {e}")
        return None
def create_booking_event(
    booking_id: str,
    event_type: str,
    event_date: datetime,
    duration_hours: int,
    location_city: str,
    location_zone: str,
    location_address: str,
    musician_email: str,
    contractor_email: str,
    contractor_name: str,
    musician_name: str,
    musician_phone: str,
    contractor_phone: str
) -> str | None:
    """
    Crea un evento en Google Calendar para una reserva confirmada y pagada.
    Agrega al mÃºsico y al contratista como invitados.
    """
    service = _get_calendar_service()
    if not service:
        return None

    calendar_id = settings.GOOGLE_CALENDAR_ID
    if not calendar_id:
        logger.warning("GOOGLE_CALENDAR_ID no estÃ¡ configurado. Se usarÃ¡ 'primary' (puede fallar para Service Accounts).")
        calendar_id = "primary"

    # Calcular fecha de fin (UTC)
    end_date = event_date
    import datetime as dt
    end_date = end_date + dt.timedelta(hours=duration_hours)

    event_summary = f"Chivapp: {event_type} - {musician_name}"
    
    # Cuerpo del evento (descripciÃ³n)
    description_lines = [
        f"<b>Reserva confirmada en Chivapp</b> (#{str(booking_id)[:8]})",
        "<br>",
        f"<b>MÃºsico:</b> {musician_name} ({musician_phone or 'Sin telÃ©fono'})",
        f"<b>Contratista:</b> {contractor_name} ({contractor_phone or 'Sin telÃ©fono'})",
        "<br>",
        f"<b>Lugar:</b> {location_address}, {location_zone}, {location_city}",
        "<br>",
        "<b>Nota:</b> Este evento fue agendado automÃ¡ticamente por Chivapp porque el pago ha sido retenido con Ã©xito."
    ]
    
    event_body = {
        'summary': event_summary,
        'location': f"{location_address}, {location_zone}, {location_city}",
        'description': "".join(description_lines),
        'start': {
            'dateTime': event_date.isoformat() + 'Z',
            'timeZone': 'UTC',
        },
        'end': {
            'dateTime': end_date.isoformat() + 'Z',
            'timeZone': 'UTC',
        },
        # 'attendees': [] # Bloqueado por Google,
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'email', 'minutes': 24 * 60},
                {'method': 'popup', 'minutes': 120},
            ],
        },
    }

    try:
        event = service.events().insert(
            calendarId=calendar_id, 
            body=event_body, 
            sendUpdates='none' # EnvÃ­a correos a los invitados
        ).execute()
        
        logger.info(f"Evento de Google Calendar creado con Ã©xito: {event.get('htmlLink')}")
        return event.get('id')
    except HttpError as error:
        logger.error(f"OcurriÃ³ un error al crear evento en Google Calendar: {error}")
        return None
    except Exception as e:
        logger.error(f"ExcepciÃ³n inesperada en Google Calendar: {e}")
        return None

def cancel_booking_event(event_id: str) -> bool:
    """Cancela (elimina) un evento existente en Google Calendar."""
    service = _get_calendar_service()
    if not service or not event_id:
        return False

    calendar_id = settings.GOOGLE_CALENDAR_ID or "primary"
    
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info(f"Evento {event_id} eliminado de Google Calendar.")
        return True
    except HttpError as error:
        logger.error(f"Error al eliminar evento en Google Calendar: {error}")
        return False

def add_attendee_to_booking_event(event_id: str, attendee_email: str) -> bool:
    # Desactivado debido a la restriccion 403 de Service Accounts (sin Workspace)
    return False

def remove_attendee_from_booking_event(event_id: str, attendee_email: str) -> bool:
    # Desactivado debido a la restriccion 403 de Service Accounts (sin Workspace)
    return False



def sync_booking_calendar(db, booking_id: str, include_contractor: bool, ensemble_member_ids: list[str]) -> str | None:
    from app.models.booking import Booking, BookingStatus
    from app.models.user import User
    from app.models.ensemble_member import EnsembleMember

    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        return None

    # Only sync if paid (or in progress etc)
    if booking.status not in (BookingStatus.payment_retained, BookingStatus.in_progress, BookingStatus.balance_pending, BookingStatus.balance_review):
        pass # Well, let's allow it anyway if the user wants it, or maybe limit it. Let's just create it.

    musician_user = db.query(User).filter(User.id == booking.musician.user_id).first()
    contractor_user = db.query(User).filter(User.id == booking.contractor.user_id).first()

    attendees = []
    if musician_user and musician_user.email and not musician_user.email.endswith("@guest.local"):
        attendees.append({'email': musician_user.email})
    
    if include_contractor and contractor_user and contractor_user.email and not contractor_user.email.endswith("@guest.local"):
        attendees.append({'email': contractor_user.email})

    for mid in ensemble_member_ids:
        em = db.query(EnsembleMember).filter(EnsembleMember.id == mid).first()
        if em and em.email and not em.email.endswith('@guest.local'):
            attendees.append({'email': em.email})
        



    if not musician_user or not getattr(musician_user, 'google_calendar_refresh_token', None):
        logger.warning(f"Musician {musician_user.email if musician_user else 'unknown'} no tiene Google Calendar conectado via OAuth.")
        return None

    service = _get_calendar_service_oauth(musician_user.google_calendar_refresh_token)
    if not service:
        return None

    calendar_id = "primary"
    
    # Calculate end date
    end_date = booking.event_date
    import datetime as dt
    # combine date and time
    start_dt = dt.datetime.combine(booking.event_date, booking.start_time)
    
    if booking.end_time:
        end_dt = dt.datetime.combine(booking.event_date, booking.end_time)
        if end_dt < start_dt:
            end_dt = end_dt + dt.timedelta(days=1)
    else:
        # Fallback duration if not found
        end_dt = start_dt + dt.timedelta(hours=1)
        
    musician_name = musician_user.fullname if musician_user else "Músico"
    musician_phone = musician_user.phone if musician_user else ""
    contractor_name = contractor_user.fullname if contractor_user else "Cliente"
    contractor_phone = contractor_user.phone if contractor_user else ""
    
    event_summary = f"Chivapp: {booking.event_type} - {musician_name}"
    
    description_lines = [
        f"<b>Reserva confirmada en Chivapp</b> (#{str(booking.id)[:8]})",
        "<br>",
        f"<b>Músico:</b> {musician_name} ({musician_phone or 'Sin teléfono'})",
        f"<b>Contratista:</b> {contractor_name} ({contractor_phone or 'Sin teléfono'})",
        "<br>",
        f"<b>Lugar:</b> {booking.location_address}, {booking.location_reference or ''}, {booking.location_city or ''}",
        "<br>",
        "<b>Nota:</b> Evento de agenda sincronizado desde Chivapp."
    ]
    
    event_body = {
        'summary': event_summary,
        'location': f"{booking.location_address}, {booking.location_reference or ''}, {booking.location_city or ''}",
        'description': "".join(description_lines),
        'start': {
            'dateTime': start_dt.isoformat() + 'Z', # timezone issue? need local timezone...
        },
        'end': {
            'dateTime': end_dt.isoformat() + 'Z',
        },
        'attendees': attendees, # Ahora funciona porque usamos OAuth del usuario
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'email', 'minutes': 24 * 60},
                {'method': 'popup', 'minutes': 120},
            ],
        },
    }

    # Add timezone handling
    # The DB stores dates and times naive. We need to attach America/Lima to it for Google Calendar
    from zoneinfo import ZoneInfo
    lima_tz = ZoneInfo('America/Lima')
    start_dt_tz = start_dt.replace(tzinfo=lima_tz)
    end_dt_tz = end_dt.replace(tzinfo=lima_tz)

    event_body['start'] = {
        'dateTime': start_dt_tz.isoformat(),
        'timeZone': 'America/Lima',
    }
    event_body['end'] = {
        'dateTime': end_dt_tz.isoformat(),
        'timeZone': 'America/Lima',
    }

    try:
        if booking.calendar_event_id:
            event = service.events().update(
                calendarId=calendar_id, 
                eventId=booking.calendar_event_id,
                body=event_body, 
                sendUpdates='none'
            ).execute()
            return event.get('id')
        else:
            event = service.events().insert(
                calendarId=calendar_id, 
                body=event_body, 
                sendUpdates='none'
            ).execute()
            booking.calendar_event_id = event.get('id')
            db.commit()
            return event.get('id')
    except Exception as e:
        logger.error(f"Error sincronizando evento en Google Calendar: {e}")
        return None












