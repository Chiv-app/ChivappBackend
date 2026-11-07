from html import escape as html_escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_participant,
    get_booking_or_404,
    parse_uuid,
)
from app.models.booking import BookingStatus
from app.models.contract import Contract
from app.models.musician_profile import MusicianProfile
from app.models.user import User, UserRole
from app.schemas.contract import ContractOut
from app.services.contract_pdf import (
    compute_contract_body_sha256,
    render_contract_pdf_bytes,
)
from app.services.uploads import read_upload_bytes

router = APIRouter(prefix="/contracts", tags=["Contracts"])

_PAID_OR_LATER_STATUSES = {
    BookingStatus.payment_retained,
    BookingStatus.change_pending,
    BookingStatus.in_progress,
    BookingStatus.payment_released,
    BookingStatus.completed,
}


def _redact_text_values(text: str | None, values_to_mask: dict[str, str]) -> str | None:
    if not text:
        return text
    redacted = text
    for raw_val, replacement in values_to_mask.items():
        cleaned = (raw_val or "").strip()
        if not cleaned or cleaned in {"—", "-", "Cliente"}:
            continue
        for candidate in {cleaned, html_escape(cleaned)}:
            if candidate:
                redacted = redacted.replace(candidate, replacement)
    return redacted


def _redact_contract_for_viewer(
    contract: Contract,
    booking,
    viewer: User,
) -> ContractOut:
    out = ContractOut.model_validate(contract)
    canonical_sha = compute_contract_body_sha256(contract.title, contract.body)
    if not out.body_sha256 and (contract.title or contract.body):
        out.body_sha256 = canonical_sha

    if viewer.role == UserRole.admin:
        return out

    ctx = dict(out.context or {})
    body = out.body
    is_paid = getattr(booking, "status", None) in _PAID_OR_LATER_STATUSES
    values_to_mask: dict[str, str] = {}

    if not is_paid:
        # L-07 (WP-08): Ocultar teléfono y correo de la contraparte hasta confirmar el pago.
        if viewer.role == UserRole.musician:
            for key in ("{{telefono_cliente}}", "{{correo_cliente}}", "contractor_phone", "contractor_email"):
                if key in ctx or key.startswith("{{"):
                    orig = str(ctx.get(key) or "")
                    if orig and orig != "—":
                        values_to_mask[orig] = "[Visible tras confirmar el pago]"
                    if key in ctx or key.startswith("{{"):
                        ctx[key] = "[Visible tras confirmar el pago]"
        elif viewer.role == UserRole.contractor:
            for key in ("{{telefono_artista}}", "{{correo_artista}}", "musician_phone", "musician_email"):
                if key in ctx or key.startswith("{{"):
                    orig = str(ctx.get(key) or "")
                    if orig and orig != "—":
                        values_to_mask[orig] = "[Visible tras confirmar el pago]"
                    if key in ctx or key.startswith("{{"):
                        ctx[key] = "[Visible tras confirmar el pago]"

    if viewer.role == UserRole.musician and not contract.contractor_signed:
        # S-01 (WP-08): Redactar datos sensibles del cliente hasta que acepte/firme.
        for key in (
            "{{documento_cliente}}",
            "{{direccion_cliente}}",
            "contractor_document_number",
            "contractor_address",
        ):
            if key in ctx or key.startswith("{{"):
                orig = str(ctx.get(key) or "")
                if orig and orig != "—":
                    values_to_mask[orig] = "[Pendiente de aceptación del cliente]"
                if key in ctx or key.startswith("{{"):
                    ctx[key] = "[Pendiente de aceptación del cliente]"

    if viewer.role == UserRole.contractor and not contract.contractor_signed:
        # S-03 (WP-08): No exponer la URL cruda de la firma del músico antes de que ambos firmen.
        out.musician_signature_url = None

    if values_to_mask:
        body = _redact_text_values(body, values_to_mask)

    out.context = ctx
    out.body = body
    out.body_sha256 = canonical_sha
    return out


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
    return musician.signature_image_url


def _ensure_booking_contract(db: Session, booking, *, commit: bool = True) -> Contract | None:
    contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if contract and (contract.contractor_signed or (contract.title and contract.body)):
        return contract
    from app.models.booking import BookingStatus
    from app.models.contractor_profile import ContractorProfile
    from app.services.booking_contract import (
        build_booking_contract_snapshot,
        create_booking_contract,
    )

    if booking.status == BookingStatus.requested:
        return None
    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    contractor = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if not musician or not contractor or not musician.user or not contractor.user:
        return contract
    if contract and not contract.contractor_signed:
        title, body, context = build_booking_contract_snapshot(
            booking,
            musician,
            musician.user,
            contractor,
            contractor.user,
        )
        contract.title = title
        contract.body = body
        contract.context = context
        db.add(contract)
        if commit:
            db.commit()
            db.refresh(contract)
        else:
            db.flush()
        return contract
    prev_status = booking.status
    contract = create_booking_contract(
        db,
        booking,
        musician=musician,
        musician_user=musician.user,
        contractor=contractor,
        contractor_user=contractor.user,
    )
    booking.status = prev_status
    if commit:
        db.commit()
        db.refresh(contract)
    else:
        db.flush()
    return contract


@router.get("/booking/{booking_id}", response_model=ContractOut)
def get_booking_contract(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    contract = _ensure_booking_contract(db, booking)
    if not contract:
        raise HTTPException(404, "Contrato no encontrado para esta reserva")

    return _redact_contract_for_viewer(contract, booking, current_user)


def render_booking_contract_pdf_response(
    db: Session,
    booking,
    booking_id: str,
    *,
    viewer: User | None = None,
) -> Response:
    """Genera el PDF desde el snapshot de la solicitud (o sirve legacy si aplica).

    Sin chequeo de autorización: cada endpoint que la llama aplica su propio guard.
    """
    contract = _ensure_booking_contract(db, booking)
    if not contract:
        raise HTTPException(404, "Contrato no encontrado para esta reserva")

    musician_signature_url = _resolve_musician_signature_url(db, booking, contract)
    effective_body = contract.body
    effective_context = dict(contract.context or {})
    if viewer is not None and viewer.role != UserRole.admin:
        redacted = _redact_contract_for_viewer(contract, booking, viewer)
        effective_body = redacted.body
        effective_context = dict(redacted.context or {})

    filename_base = f"contrato-{booking_id}"
    if contract.title and effective_body:
        try:
            pdf_bytes = render_contract_pdf_bytes(
                title=contract.title,
                body=effective_body,
                context=effective_context,
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
    return render_booking_contract_pdf_response(
        db,
        booking,
        booking_id,
        viewer=current_user,
    )


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
    return _redact_contract_for_viewer(contract, booking, current_user)
