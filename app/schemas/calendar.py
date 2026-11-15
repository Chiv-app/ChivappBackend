from datetime import date

from pydantic import BaseModel, Field


class CalendarEventOut(BaseModel):
    """Un compromiso de la agenda de la persona."""

    id: str
    booking_id: str
    # «booking» (reserva propia) o «convocation» (convocada como integrante de otro grupo).
    kind: str
    # «owner» (dueña de la reserva), «member» (integrante) o «contractor» (quien contrata).
    role: str
    # «confirmed» (pago retenido o más adelante) o «tentative» (aún sin pago o sin responder).
    state: str
    title: str
    event_type: str | None = None
    start: str
    end: str
    city: str | None = None
    address: str | None = None
    profile_id: str | None = None
    profile_name: str | None = None
    # La reserva ya se envió a Google Calendar.
    synced: bool = False
    # Se cruza con otro compromiso de la misma persona.
    conflict: bool = False


class CalendarEventsOut(BaseModel):
    start: date
    end: date
    events: list[CalendarEventOut] = Field(default_factory=list)


class BusyBlockOut(BaseModel):
    start: str
    end: str


class CalendarBusyOut(BaseModel):
    start: date
    end: date
    # Hay una cuenta de Google conectada.
    connected: bool = False
    # Google rechazó el permiso de horas ocupadas: hay que volver a conectar.
    needs_reconnect: bool = False
    busy: list[BusyBlockOut] = Field(default_factory=list)
