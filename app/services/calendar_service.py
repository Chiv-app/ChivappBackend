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
        'attendees': [
            {'email': musician_email},
            {'email': contractor_email},
        ],
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
            sendUpdates='all' # EnvÃ­a correos a los invitados
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
    """Agrega un asistente a un evento de Google Calendar."""
    service = _get_calendar_service()
    if not service or not event_id:
        return False
    calendar_id = settings.GOOGLE_CALENDAR_ID or "primary"
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = event.get('attendees', [])
        if any(a.get('email') == attendee_email for a in attendees):
            return True
        attendees.append({'email': attendee_email})
        event['attendees'] = attendees
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event, sendUpdates='all').execute()
        return True
    except Exception as e:
        logger.error(f"Error agregando asistente al calendario: {e}")
        return False

def remove_attendee_from_booking_event(event_id: str, attendee_email: str) -> bool:
    """Elimina un asistente de un evento de Google Calendar."""
    service = _get_calendar_service()
    if not service or not event_id:
        return False
    calendar_id = settings.GOOGLE_CALENDAR_ID or "primary"
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        attendees = event.get('attendees', [])
        new_attendees = [a for a in attendees if a.get('email') != attendee_email]
        if len(attendees) == len(new_attendees):
            return True
        event['attendees'] = new_attendees
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event, sendUpdates='all').execute()
        return True
    except Exception as e:
        logger.error(f"Error eliminando asistente del calendario: {e}")
        return False



