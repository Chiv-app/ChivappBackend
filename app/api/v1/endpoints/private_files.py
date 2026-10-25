"""Descarga de archivos privados (DNI, firmas, comprobantes) con control de acceso."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.api import deps
from app.models.booking import Booking
from app.models.booking_complaint import BookingComplaint
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment
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
    """Dueño, admin, o contraparte de una reserva con el dueño cuando el archivo
    pertenece a esa reserva compartida (contrato, firma, comprobante o queja)."""
    if user.role == UserRole.admin or str(record.owner_user_id) == str(user.id):
        return True
    file_url = f"/uploads/private/{record.filename}"
    is_identity_doc = (
        db.query(MusicianProfile.id)
        .filter(
            MusicianProfile.user_id == record.owner_user_id,
            MusicianProfile.id_document_url == file_url,
        )
        .first()
        is not None
        or db.query(ContractorProfile.id)
        .filter(
            ContractorProfile.user_id == record.owner_user_id,
            ContractorProfile.id_document_url == file_url,
        )
        .first()
        is not None
    )
    if is_identity_doc:
        return False
    owner_musician, owner_contractor = _profile_ids(db, record.owner_user_id)
    user_musician, user_contractor = _profile_ids(db, user.id)
    conditions = []
    if owner_musician and user_contractor:
        conditions.append(and_(Booking.musician_id.in_(owner_musician), Booking.contractor_id.in_(user_contractor)))
    if owner_contractor and user_musician:
        conditions.append(and_(Booking.contractor_id.in_(owner_contractor), Booking.musician_id.in_(user_musician)))
    if not conditions:
        return False
    shared_booking_ids = [row.id for row in db.query(Booking.id).filter(or_(*conditions)).all()]
    if not shared_booking_ids:
        return False

    # 1. Referenciado en el contrato de una reserva compartida
    if (
        db.query(Contract.id)
        .filter(
            Contract.booking_id.in_(shared_booking_ids),
            or_(
                Contract.musician_signature_url == file_url,
                Contract.contractor_signature_url == file_url,
                Contract.contract_pdf_url == file_url,
            ),
        )
        .first()
        is not None
    ):
        return True

    # 2. Referenciado en un pago de una reserva compartida
    shared_payments = (
        db.query(Payment.evidence_url, Payment.payout_evidence_url, Payment.evidence_urls)
        .filter(Payment.booking_id.in_(shared_booking_ids))
        .all()
    )
    for pay_row in shared_payments:
        if pay_row.evidence_url == file_url or pay_row.payout_evidence_url == file_url:
            return True
        if isinstance(pay_row.evidence_urls, list) and file_url in pay_row.evidence_urls:
            return True

    # 3. Referenciado en una queja de una reserva compartida
    if (
        db.query(BookingComplaint.id)
        .filter(
            BookingComplaint.booking_id.in_(shared_booking_ids),
            or_(
                BookingComplaint.evidence_url == file_url,
                BookingComplaint.musician_response_evidence_url == file_url,
                BookingComplaint.refund_evidence_url == file_url,
            ),
        )
        .first()
        is not None
    ):
        return True

    # 4. Firma o plantilla PDF activa del perfil con quien comparte reserva
    if owner_musician and (
        db.query(MusicianProfile.id)
        .filter(
            MusicianProfile.id.in_(owner_musician),
            or_(
                MusicianProfile.signature_image_url == file_url,
                MusicianProfile.contract_pdf_url == file_url,
            ),
        )
        .first()
        is not None
    ):
        return True
    if owner_contractor and (
        db.query(ContractorProfile.id)
        .filter(
            ContractorProfile.id.in_(owner_contractor),
            ContractorProfile.contract_pdf_url == file_url,
        )
        .first()
        is not None
    ):
        return True

    return False


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
