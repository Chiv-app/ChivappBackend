from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api import deps
from app.models.user import User, UserRole
from app.schemas.calendar import CalendarBusyOut, CalendarEventsOut
from app.services.calendar_service import get_busy_blocks
from app.services.person_calendar import CalendarRange, list_calendar_events, subtract_synced_events

router = APIRouter(prefix="/calendar", tags=["Calendar"])


def _range_or_400(start: date, end: date) -> CalendarRange:
    try:
        return CalendarRange.parse(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _assert_calendar_user(user: User) -> None:
    if user.role not in (UserRole.musician, UserRole.contractor):
        raise HTTPException(status_code=403, detail="El calendario es para músicos y contratantes.")


@router.get("/events", response_model=CalendarEventsOut)
def get_calendar_events(
    start: date = Query(...),
    end: date = Query(...),
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Agenda de la persona en un rango: sus reservas (de todos sus perfiles), convocatorias y cruces."""
    _assert_calendar_user(current_user)
    rng = _range_or_400(start, end)
    return CalendarEventsOut(start=rng.start, end=rng.end, events=list_calendar_events(db, current_user, rng))


@router.get("/busy", response_model=CalendarBusyOut)
def get_calendar_busy(
    start: date = Query(...),
    end: date = Query(...),
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Horas que la persona ya tiene ocupadas en Google (solo inicio y fin). Se consulta al abrir el calendario."""
    _assert_calendar_user(current_user)
    rng = _range_or_400(start, end)
    result = get_busy_blocks(
        current_user,
        datetime.combine(rng.start, time.min),
        datetime.combine(rng.end + timedelta(days=1), time.min),
    )
    busy = result.busy
    if busy:
        busy = subtract_synced_events(busy, list_calendar_events(db, current_user, rng))
    return CalendarBusyOut(
        start=rng.start,
        end=rng.end,
        connected=result.connected,
        needs_reconnect=result.needs_reconnect,
        busy=busy,
    )
