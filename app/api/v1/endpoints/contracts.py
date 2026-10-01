from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_participant,
    get_booking_or_404,
    parse_uuid,
)
from app.models.contract import Contract
from app.models.musician_profile import MusicianProfile
from app.models.user import User
from app.schemas.contract import ContractOut
from app.services.contract_pdf import render_contract_pdf_bytes
from app.services.uploads import read_upload_bytes

router = APIRouter(prefix="/contracts", tags=["Contracts"])


def _legacy_pdf_bytes(upload_url: str | None) -> bytes | None:
    return read_upload_bytes(upload_url)


def _resolve_musician_signature_url(
    db: Session,
    booking,
    contract: Contract,
) -> str | None:
    if contract.musician_signature_url:
        return contract.musician_signature_url
    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if not musician or not musician.signature_image_url:
        return None
    contract.musician_signature_url = musician.signature_image_url
    db.add(contract)
    db.commit()
    return contract.musician_signature_url


@router.get("/booking/{booking_id}", response_model=ContractOut)
def get_booking_contract(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if not contract:
        raise HTTPException(404, "Contrato no encontrado para esta reserva")

    if not contract.musician_signature_url:
        _resolve_musician_signature_url(db, booking, contract)
        db.refresh(contract)

    return contract


def render_booking_contract_pdf_response(
    db: Session,
    booking,
    booking_id: str,
) -> Response:
    """Genera el PDF desde el snapshot de la solicitud (o sirve legacy si aplica).

    Sin chequeo de autorización: cada endpoint que la llama aplica su propio guard.
    """
    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if not contract:
        raise HTTPException(404, "Contrato no encontrado para esta reserva")

    musician_signature_url = _resolve_musician_signature_url(db, booking, contract)

    filename_base = f"contrato-{booking_id}"
    if contract.title and contract.body:
        try:
            pdf_bytes = render_contract_pdf_bytes(
                title=contract.title,
                body=contract.body,
                context=contract.context or {},
                signature_image_url=contract.contractor_signature_url,
                musician_signature_url=musician_signature_url,
                signed_at=contract.contractor_sign_timestamp,
                sign_ip=contract.contractor_sign_ip,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if contract.contractor_signed:
            filename_base = f"contrato-firmado-{booking_id}"
    else:
        legacy_url = (
            contract.contract_signed_pdf_url or contract.contract_pdf_url
        )
        pdf_bytes = _legacy_pdf_bytes(legacy_url)
        if pdf_bytes is None:
            raise HTTPException(
                404,
                "Este contrato no tiene contenido ni PDF disponible",
            )
        if contract.contract_signed_pdf_url:
            filename_base = f"contrato-firmado-{booking_id}"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename_base}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/booking/{booking_id}/pdf")
def get_booking_contract_pdf(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)
    return render_booking_contract_pdf_response(db, booking, booking_id)


@router.get("/{contract_id}", response_model=ContractOut)
def get_contract(
    contract_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    contract = db.get(Contract, parse_uuid(contract_id, "contract_id"))
    if not contract:
        raise HTTPException(404, "Contrato no encontrado")

    booking = get_booking_or_404(db, str(contract.booking_id))
    assert_booking_participant(db, booking, current_user)
    return contract
