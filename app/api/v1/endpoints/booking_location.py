from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_collaborator,
    get_booking_or_404,
)
from app.models.booking_location import BookingLocationPing
from app.models.user import User, UserRole
from app.schemas.booking_location import (
    LiveLocationCoords,
    LiveLocationPingOut,
    LiveLocationSessionOut,
)
from app.services.booking_location import (
    get_session,
    list_notify_targets,
    request_others,
    resolve_party,
    start_sharing,
    stop_sharing,
    update_position,
)
from app.services.booking_notifications import (
    notify_live_location_requested,
    notify_live_location_sharing,
)

router = APIRouter(prefix="/bookings", tags=["Booking live location"])


@router.get("/{booking_id}/live-location", response_model=LiveLocationSessionOut)
def get_live_location(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    session = get_session(db, booking=booking, user=current_user)
    db.commit()
    return session


@router.post("/{booking_id}/live-location/share", response_model=LiveLocationSessionOut)
def share_live_location(
    booking_id: str,
    payload: LiveLocationCoords,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    party = resolve_party(db, booking, current_user)
    session = start_sharing(db, booking=booking, user=current_user, coords=payload)
    for target in list_notify_targets(
        db, booking, exclude_user_id=current_user.id
    ):
        notify_live_location_sharing(db, target, str(booking.id), party)
    db.commit()
    return session


@router.post("/{booking_id}/live-location/update", response_model=LiveLocationSessionOut)
def update_live_location(
    booking_id: str,
    payload: LiveLocationCoords,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    session = update_position(
        db,
        booking=booking,
        user=current_user,
        coords=payload,
        action="update",
    )
    db.commit()
    return session


@router.post("/{booking_id}/live-location/refresh", response_model=LiveLocationSessionOut)
def refresh_live_location(
    booking_id: str,
    payload: LiveLocationCoords,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Actualiza GPS propio (si está compartiendo) y devuelve el estado actual."""
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    share_session = get_session(db, booking=booking, user=current_user)
    if share_session.me.sharing:
        session = update_position(
            db,
            booking=booking,
            user=current_user,
            coords=payload,
            action="refresh",
        )
    else:
        session = share_session
    db.commit()
    return session


@router.post("/{booking_id}/live-location/request", response_model=LiveLocationSessionOut)
def request_live_location(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    party = resolve_party(db, booking, current_user)
    session = request_others(db, booking=booking, user=current_user)
    for target in list_notify_targets(
        db, booking, exclude_user_id=current_user.id
    ):
        # Solo notificar a quienes aún no comparten.
        target_state = next(
            (p for p in session.participants if p.user_id == target.id),
            None,
        )
        if target_state and not target_state.sharing:
            notify_live_location_requested(db, target, str(booking.id), party)
    db.commit()
    return session


@router.post("/{booking_id}/live-location/stop", response_model=LiveLocationSessionOut)
def stop_live_location(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)
    session = stop_sharing(db, booking=booking, user=current_user)
    db.commit()
    return session


@router.get(
    "/{booking_id}/live-location/history",
    response_model=list[LiveLocationPingOut],
)
def live_location_history(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Historial de pings/acciones para análisis (participantes o admin)."""
    booking = get_booking_or_404(db, booking_id)
    if current_user.role != UserRole.admin:
        assert_booking_collaborator(db, booking, current_user)

    rows = (
        db.query(BookingLocationPing)
        .filter(BookingLocationPing.booking_id == booking.id)
        .order_by(BookingLocationPing.created_at.desc())
        .limit(2000)
        .all()
    )
    return list(reversed(rows))
