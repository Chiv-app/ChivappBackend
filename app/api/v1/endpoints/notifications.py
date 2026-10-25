from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api import deps
from app.api.booking_helpers import parse_uuid
from app.models.user import User
from app.models.notification import Notification
from app.schemas.notification import NotificationOut

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("/me", response_model=list[NotificationOut])
def list_my_notifications(
    response: Response,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    unread_count = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
        .count()
    )
    response.headers["X-Unread-Count"] = str(int(unread_count or 0))
    notifs = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return notifs


@router.post("/read-all")
def mark_all_notifications_as_read(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    updated = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
        .update({Notification.is_read: True}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "updated": int(updated or 0)}


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_notification_as_read(
    notification_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    notif = db.get(Notification, parse_uuid(notification_id, "notification_id"))
    if not notif:
        raise HTTPException(404, "Notificación no encontrada")

    if notif.user_id != current_user.id:
        raise HTTPException(403, "No puedes modificar esta notificación")

    notif.is_read = True
    db.commit()
    db.refresh(notif)
    return notif
