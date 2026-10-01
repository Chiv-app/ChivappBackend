"""Descarga de archivos privados (DNI, firmas, comprobantes) con control de acceso."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.api import deps
from app.models.booking import Booking
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.uploaded_file import UploadedFile
from app.models.user import User, UserRole
from app.services.uploads import read_private_file

# Se monta en la raíz (no bajo /api/v1) para que la URL sea /uploads/private/<archivo>.
router = APIRouter(tags=["Uploads"])

PRIVATE_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, no-store",
}


def _profile_ids(db: Session, user_id) -> tuple[list, list]:
    musician_ids = [row.id for row in db.query(MusicianProfile.id).filter(MusicianProfile.user_id == user_id)]
    contractor_ids = [row.id for row in db.query(ContractorProfile.id).filter(ContractorProfile.user_id == user_id)]
    return musician_ids, contractor_ids


def can_access_private_file(db: Session, user: User, record: UploadedFile) -> bool:
    """Dueño, admin, o contraparte de una reserva con el dueño (p. ej. para ver
    la firma en el contrato)."""
    if user.role == UserRole.admin or str(record.owner_user_id) == str(user.id):
        return True
    owner_musician, owner_contractor = _profile_ids(db, record.owner_user_id)
    user_musician, user_contractor = _profile_ids(db, user.id)
    conditions = []
    if owner_musician and user_contractor:
        conditions.append(and_(Booking.musician_id.in_(owner_musician), Booking.contractor_id.in_(user_contractor)))
    if owner_contractor and user_musician:
        conditions.append(and_(Booking.contractor_id.in_(owner_contractor), Booking.musician_id.in_(user_musician)))
    if not conditions:
        return False
    return db.query(Booking.id).filter(or_(*conditions)).first() is not None


@router.get("/uploads/private/{filename}")
def get_private_file(
    filename: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    record = db.query(UploadedFile).filter(UploadedFile.filename == filename).first()
    # 404 también cuando no hay permiso, para no revelar qué archivos existen.
    if not record or not can_access_private_file(db, current_user, record):
        raise HTTPException(404, "Archivo no encontrado")
    content = read_private_file(record.filename)
    if content is None:
        raise HTTPException(404, "Archivo no encontrado")
    disposition = "inline" if record.content_type.startswith("image/") or record.content_type == "application/pdf" else "attachment"
    return Response(
        content=content,
        media_type=record.content_type,
        headers={**PRIVATE_FILE_HEADERS, "Content-Disposition": f'{disposition}; filename="{record.filename}"'},
    )
