from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog

logger = logging.getLogger(__name__)

# IP del cliente de la petición en curso; la fija `deps.get_current_admin` para que toda
# acción de admin quede auditada con su origen sin pasar la IP en cada llamada.
_request_ip: ContextVar[str | None] = ContextVar("admin_audit_request_ip", default=None)


def set_audit_request_ip(ip: str | None) -> None:
    _request_ip.set(ip)


def record_admin_audit(
    db: Session,
    *,
    admin_user: Any | None = None,
    admin_user_id: UUID | str | None = None,
    action: str,
    target_type: str,
    target_id: UUID | str | None = None,
    before_data: dict[str, Any] | None = None,
    before_state: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    ip_address: str | None = None,
    commit: bool = False,
) -> AdminAuditLog | None:
    """Registra una entrada en `admin_audit_log` para trazabilidad administrativa (WP-07)."""
    try:
        resolved_admin_id = admin_user_id or (getattr(admin_user, "id", None) if admin_user is not None else None)
        uid = UUID(str(resolved_admin_id)) if resolved_admin_id else None
        entry = AdminAuditLog(
            admin_user_id=uid,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            before_data=before_data if before_data is not None else before_state,
            after_data=after_data if after_data is not None else after_state,
            ip_address=ip_address or _request_ip.get(),
        )
        db.add(entry)
        if commit:
            db.commit()
            db.refresh(entry)
        else:
            db.flush()
        return entry
    except Exception as exc:
        # Es un fallo de trazabilidad: se deja en nivel ERROR para que salte en los registros.
        logger.error("No se pudo registrar auditoría admin (%s/%s): %s", action, target_type, exc)
        return None
