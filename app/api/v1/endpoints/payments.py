import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.limiter import get_client_ip, limiter

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_contractor_owner,
    assert_booking_participant,
    get_booking_or_404,
    get_contractor_profile,
    get_musician_profile_or_400,
    parse_uuid,
)
from app.models.user import User, UserRole
from app.models.payment import Payment, PaymentStatus
from app.models.booking import Booking, BookingStatus
from app.models.musician_profile import MusicianProfile
from app.models.contract import Contract
from app.schemas.payment import (
    ContractorExpensesItem,
    ContractorExpensesSummary,
    ContractorOperationsSummary,
    MercadoPagoPaymentCheckResponse,
    MercadoPagoPreferenceRequest,
    MercadoPagoPreferenceResponse,
    MercadoPagoProcessPaymentRequest,
    MercadoPagoProcessPaymentResponse,
    MusicianDebtItem,
    MusicianEarningsItem,
    MusicianEarningsSummary,
    PaymentOut,
)
from app.services.booking_notifications import notify_admins, notify_payment_released
from app.services.contractor_operations import build_contractor_operations
from app.services.payment_evidence import (
    apply_evidence_urls,
    assert_evidence_uploads,
    normalize_evidence_urls,
)
from app.services import mercadopago_service
from app.services.uploads import assert_private_upload_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.get("/platform-instructions")
def get_platform_payment_instructions(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from app.schemas.settlement import PlatformPaymentInstructionsOut
    from app.services.platform_payment import get_or_create_platform_payment_settings

    _ = current_user
    settings_row = get_or_create_platform_payment_settings(db)
    db.commit()
    return PlatformPaymentInstructionsOut.model_validate(settings_row)


def _get_payment_or_404(db: Session, payment_id: str) -> Payment:
    payment = db.get(Payment, parse_uuid(payment_id, "payment_id"))
    if not payment:
        raise HTTPException(404, "Pago no encontrado")
    return payment


def _get_booking_payment_or_404(db: Session, booking_id: str) -> tuple[Booking, Payment]:
    booking = get_booking_or_404(db, booking_id)
    payment = (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id)
        .order_by(Payment.created_at.desc())
        .first()
    )
    if not payment:
        raise HTTPException(404, "No hay pago registrado para esta reserva")
    return booking, payment


@router.get("/booking/{booking_id}", response_model=PaymentOut)
def get_booking_payment(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking, payment = _get_booking_payment_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)
    return payment


@router.get("/booking/{booking_id}/all", response_model=list[PaymentOut])
def list_booking_payments(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)
    return (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id)
        .order_by(Payment.created_at.asc())
        .all()
    )


@router.get("/musician/earnings", response_model=MusicianEarningsSummary)
def get_musician_earnings(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    return compute_musician_earnings(db=db, current_user=current_user, require_verified=True)


def compute_musician_earnings(
    db: Session,
    current_user: User,
    *,
    require_verified: bool = True,
) -> MusicianEarningsSummary:
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo los músicos pueden ver sus ingresos")

    from app.models.booking_complaint import BookingComplaint
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import settlement_state_for_booking

    musician = get_musician_profile_or_400(db, current_user, require_verified=require_verified)
    bookings = (
        db.query(Booking)
        .filter(Booking.musician_id == musician.id)
        .order_by(Booking.event_date.desc())
        .all()
    )

    from app.models.booking import BookingReview
    from app.models.contractor_recommendation import ContractorRecommendation
    from app.services.booking_lifecycle import is_event_finished

    booking_by_id = {booking.id: booking for booking in bookings}

    review_rows = (
        db.query(BookingReview)
        .filter(
            BookingReview.booking_id.in_(booking_by_id.keys()),
            (BookingReview.is_final.is_(True))
            | (BookingReview.author_user_id.isnot(None)),
        )
        .all()
        if booking_by_id
        else []
    )
    contractor_review_booking_ids: set = set()
    for rev in review_rows:
        b_obj = booking_by_id.get(rev.booking_id)
        if not b_obj:
            continue
        rev_dt = (
            rev.created_at.replace(tzinfo=timezone.utc)
            if rev.created_at and rev.created_at.tzinfo is None
            else rev.created_at
        )
        if rev.is_final:
            if is_event_finished(b_obj):
                contractor_review_booking_ids.add(rev.booking_id)
        elif rev.emoji is None and is_event_finished(b_obj, now=rev_dt):
            contractor_review_booking_ids.add(rev.booking_id)

    musician_review_booking_ids = (
        {
            row[0]
            for row in db.query(ContractorRecommendation.booking_id)
            .filter(ContractorRecommendation.booking_id.in_(booking_by_id.keys()))
            .all()
        }
        if booking_by_id
        else set()
    )

    # Determinar el estado efectivo sin mutar la base de datos en un GET (BUG-14).
    # Una reserva se considera cerrada cuando el contratista dejó su reseña Y el músico dejó la suya.
    effective_status_by_id: dict = {}
    for booking in bookings:
        if (
            booking.status
            in {
                BookingStatus.in_progress,
                BookingStatus.payment_retained,
                BookingStatus.payment_released,
            }
            and is_event_finished(booking)
            and booking.id in contractor_review_booking_ids
            and booking.id in musician_review_booking_ids
        ):
            effective_status_by_id[booking.id] = BookingStatus.completed
        else:
            effective_status_by_id[booking.id] = booking.status

    total_quoted = Decimal("0.00")
    bookings_active = 0
    bookings_completed = 0
    bookings_cancelled = 0

    for booking in bookings:
        eff_status = effective_status_by_id[booking.id]
        if eff_status == BookingStatus.cancelled:
            bookings_cancelled += 1
            continue
        if eff_status == BookingStatus.completed:
            bookings_completed += 1
        elif eff_status not in {BookingStatus.requested}:
            bookings_active += 1
        if booking.price_agreed is not None:
            total_quoted += Decimal(str(booking.price_agreed))

    payments = (
        db.query(Payment)
        .filter(Payment.booking_id.in_(booking_by_id.keys()) if booking_by_id else False)
        .order_by(Payment.created_at.desc())
        .all()
        if booking_by_id
        else []
    )

    complaints = (
        db.query(BookingComplaint)
        .filter(
            BookingComplaint.booking_id.in_(booking_by_id.keys())
            if booking_by_id
            else False
        )
        .all()
        if booking_by_id
        else []
    )
    complaint_by_booking = {c.booking_id: c for c in complaints}

    payments_by_booking: dict = {}
    for payment in payments:
        payments_by_booking.setdefault(payment.booking_id, []).append(payment)

    total_released = Decimal("0.00")
    total_retained = Decimal("0.00")
    total_pending = Decimal("0.00")
    total_app_debt = Decimal("0.00")
    total_disputed = Decimal("0.00")
    total_retained_active = Decimal("0.00")
    items: list[MusicianEarningsItem] = []
    debts: list[MusicianDebtItem] = []

    for payment in payments:
        booking = booking_by_id.get(payment.booking_id)
        if not booking:
            continue
        eff_status = effective_status_by_id[booking.id]
        amount_dec = musician_portion_of_paid(booking, Decimal(str(payment.amount)))
        amount = float(amount_dec)
        if payment.status == PaymentStatus.released:
            total_released += amount_dec
        elif payment.status == PaymentStatus.retained:
            total_retained += amount_dec
            is_fully_culminated = eff_status == BookingStatus.completed
            if not is_fully_culminated:
                total_retained_active += amount_dec
        elif payment.status == PaymentStatus.initiated:
            is_needs_refund = (
                isinstance(payment.gateway_metadata, dict)
                and bool(payment.gateway_metadata.get("needs_refund"))
            )
            if (
                eff_status not in {BookingStatus.cancelled, BookingStatus.completed}
                and not is_needs_refund
            ):
                total_pending += amount_dec

        items.append(
            MusicianEarningsItem(
                payment_id=payment.id,
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                booking_status=eff_status.value,
                amount=amount,
                currency=payment.currency or "PEN",
                payment_type=payment.payment_type,
                status=payment.status,
                retained_at=payment.retained_at,
                released_at=payment.released_at,
                created_at=payment.created_at,
            )
        )

    for booking in bookings:
        eff_status = effective_status_by_id[booking.id]
        if eff_status == BookingStatus.cancelled:
            continue
        booking_payments = payments_by_booking.get(booking.id, [])
        retained_gross = sum(
            (Decimal(str(p.amount or 0)) for p in booking_payments if p.status == PaymentStatus.retained),
            Decimal("0.00"),
        )
        released_gross = sum(
            (Decimal(str(p.amount or 0)) for p in booking_payments if p.status == PaymentStatus.released),
            Decimal("0.00"),
        )
        retained_dec = musician_portion_of_paid(booking, retained_gross)
        released_dec = musician_portion_of_paid(booking, released_gross)
        retained = float(retained_dec)
        released = float(released_dec)
        if retained <= 0 and released <= 0 and eff_status != BookingStatus.completed:
            continue

        complaint = complaint_by_booking.get(booking.id)
        state = settlement_state_for_booking(
            booking,
            complaint,
            retained,
            released,
            effective_status=eff_status,
        )
        if state == "none":
            continue

        has_contractor_rev = booking.id in contractor_review_booking_ids
        has_musician_rev = booking.id in musician_review_booking_ids

        if state == "payable":
            total_app_debt += retained_dec
        elif state in {"disputed", "awaiting_admin"}:
            total_disputed += retained_dec

        debts.append(
            MusicianDebtItem(
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                price_agreed=(
                    float(booking.price_agreed) if booking.price_agreed is not None else None
                ),
                retained_total=retained,
                released_total=released,
                currency="PEN",
                booking_status=eff_status.value,
                debt_state=state,
                has_contractor_review=has_contractor_rev,
                has_musician_review=has_musician_rev,
                complaint_status=complaint.status.value if complaint else None,
                complaint_reason=complaint.reason if complaint else None,
                admin_musician_amount=(
                    float(complaint.admin_musician_amount)
                    if complaint and complaint.admin_musician_amount is not None
                    else None
                ),
                admin_contractor_refund=(
                    float(complaint.admin_contractor_refund)
                    if complaint and complaint.admin_contractor_refund is not None
                    else None
                ),
            )
        )

    pending_payout_out: PayoutRequestOut | None = None
    try:
        pending_req = (
            db.query(PayoutRequest)
            .filter(
                PayoutRequest.musician_id == musician.id,
                PayoutRequest.status == "pending",
            )
            .order_by(PayoutRequest.created_at.desc())
            .first()
        )
        if pending_req and total_app_debt > 0:
            pending_payout_out = PayoutRequestOut.model_validate(pending_req)
    except Exception as exc:
        logger.warning("No se pudo consultar payout_request: %s", exc)
        db.rollback()

    return MusicianEarningsSummary(
        currency="PEN",
        total_quoted=float(total_quoted),
        total_released=float(total_released),
        total_retained=float(total_retained),
        total_pending=float(total_pending),
        total_app_debt=float(total_app_debt),
        total_disputed=float(total_disputed),
        total_retained_active=float(total_retained_active),
        bookings_active=bookings_active,
        bookings_completed=bookings_completed,
        bookings_cancelled=bookings_cancelled,
        items=items,
        debts=debts,
        pending_payout_request=pending_payout_out,
    )


@router.get("/contractor/operations", response_model=ContractorOperationsSummary)
def get_contractor_operations(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Unified contractor feed: pendientes, dinero, disputas y reembolsos."""
    if current_user.role != UserRole.contractor:
        raise HTTPException(
            403, "Solo los contratistas pueden ver sus operaciones"
        )

    contractor = get_contractor_profile(db, current_user)
    if not contractor:
        raise HTTPException(400, "Debes tener un perfil de contratista creado")

    return build_contractor_operations(db, contractor.id)


@router.get("/contractor/expenses", response_model=ContractorExpensesSummary)
def get_contractor_expenses(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Legacy read-model; prefer /contractor/operations."""
    if current_user.role != UserRole.contractor:
        raise HTTPException(403, "Solo los contratistas pueden ver sus egresos")

    contractor = get_contractor_profile(db, current_user)
    if not contractor:
        raise HTTPException(400, "Debes tener un perfil de contratista creado")

    ops = build_contractor_operations(db, contractor.id)
    payment_items: list[ContractorExpensesItem] = []
    for item in ops.items:
        if item.source != "payment" or item.payment_status is None:
            continue
        payment_id = item.id.removeprefix("payment:")
        payment_items.append(
            ContractorExpensesItem(
                payment_id=parse_uuid(payment_id),
                booking_id=item.booking_id,
                event_type=item.event_type,
                event_date=item.event_date,
                location_city=item.location_city,
                musician_stage_name=item.musician_stage_name,
                booking_status=item.booking_status,
                amount=item.amount or 0,
                currency=item.currency,
                payment_type=item.payment_type,
                status=item.payment_status,
                retained_at=None,
                released_at=None,
                created_at=item.occurred_at,
            )
        )

    return ContractorExpensesSummary(
        currency=ops.currency,
        total_quoted=ops.total_quoted,
        total_released=ops.total_released,
        total_retained=ops.total_retained,
        total_pending=ops.total_pending,
        bookings_active=ops.bookings_active,
        bookings_completed=ops.bookings_completed,
        bookings_cancelled=ops.bookings_cancelled,
        items=payment_items,
    )


@router.post("/{payment_id}/retain", response_model=PaymentOut)
def retain_payment(
    payment_id: str,
    current_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    # Solo admin: los pagos se confirman por el webhook de Mercado Pago.
    payment = _get_payment_or_404(db, payment_id)
    booking = get_booking_or_404(db, str(payment.booking_id))

    if payment.status != PaymentStatus.initiated:
        raise HTTPException(400, "El pago no está en estado 'initiated'")
    if isinstance(payment.gateway_metadata, dict) and payment.gateway_metadata.get("needs_refund"):
        raise HTTPException(400, "No se puede retener un pago marcado para reembolso o revisión")
    if booking.status in {BookingStatus.cancelled, BookingStatus.completed}:
        raise HTTPException(400, "No se puede retener el pago de una reserva cerrada o cancelada")
    if booking.status not in {
        BookingStatus.accepted,
        BookingStatus.contract_pending,
        BookingStatus.contract_signed,
        BookingStatus.payment_pending,
    }:
        raise HTTPException(400, "La reserva no está en un estado que permita retener un pago")

    from app.services.availability_match import assert_no_booking_overlap

    if booking.musician_id and booking.event_date and booking.start_time:
        assert_no_booking_overlap(
            db,
            booking.musician_id,
            booking.event_date,
            booking.start_time,
            end_time=booking.end_time,
            exclude_booking_id=booking.id,
        )

    payment.status = PaymentStatus.retained
    payment.retained_at = datetime.utcnow()
    booking.status = BookingStatus.payment_retained

    db.commit()
    db.refresh(payment)

    return payment


@router.post("/{payment_id}/release", response_model=PaymentOut)
def release_payment(
    payment_id: str,
    current_user: User = Depends(deps.get_current_admin),
    db: Session = Depends(deps.get_db),
):
    # Solo admin: los pagos se confirman por el webhook de Mercado Pago.
    payment = _get_payment_or_404(db, payment_id)
    booking = get_booking_or_404(db, str(payment.booking_id))

    if payment.status != PaymentStatus.retained:
        raise HTTPException(400, "El pago debe estar retenido antes de liberarse")
    if booking.status == BookingStatus.cancelled:
        raise HTTPException(400, "Usa el flujo de liquidación para liberar pagos de reservas canceladas")

    payment.status = PaymentStatus.released
    payment.released_at = datetime.utcnow()
    if booking.status != BookingStatus.completed:
        booking.status = BookingStatus.payment_released

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if musician:
        musician_user = db.query(User).filter(User.id == musician.user_id).first()
        if musician_user:
            notify_payment_released(db, musician_user, str(booking.id))

    db.commit()
    db.refresh(payment)

    return payment


# Pago único (100 %) por Mercado Pago.
PAYABLE_STATUSES = {
    "full": (BookingStatus.accepted, BookingStatus.contract_pending, BookingStatus.contract_signed, BookingStatus.payment_pending),
}


def _server_amount(db: Session, booking: Booking, payment_type: str, client_amount) -> float:
    """Calcula el monto en el servidor. El monto del cliente solo se usa para
    detectar que la pantalla quedó desactualizada, nunca para cobrar."""
    allowed = PAYABLE_STATUSES.get(payment_type)
    if allowed is None:
        raise HTTPException(400, "Tipo de pago no soportado")
    if booking.status not in allowed:
        raise HTTPException(400, "La reserva no está en un estado que permita este pago")
    from app.services.booking_lifecycle import is_event_started
    if is_event_started(booking):
        raise HTTPException(400, "No se puede procesar el pago porque la fecha u hora del evento ya pasó.")
    from app.services.availability_match import assert_no_booking_overlap

    assert_no_booking_overlap(
        db,
        booking.musician_id,
        booking.event_date,
        booking.start_time,
        end_time=booking.end_time,
        exclude_booking_id=booking.id,
    )
    try:
        expected = mercadopago_service.expected_payment_amount(db, booking, payment_type)
    except ValueError as exc:
        db.rollback()
        logger.warning("Error procesando pago directo de Mercado Pago para reserva %s: %s", booking.id, exc)
        msg = str(exc)
        safe_detail = (
            msg
            if any(
                token in msg.lower()
                for token in ("teléfono", "otp", "token", "monto", "moneda", "no está configurad")
            )
            else "No se pudo procesar el pago con la pasarela. Verifica los datos ingresados e inténtalo nuevamente."
        )
        raise HTTPException(400, detail=safe_detail) from exc
    if expected <= 0:
        raise HTTPException(400, "El monto a pagar debe ser mayor a cero")
    if client_amount and abs(Decimal(str(client_amount)) - expected) > mercadopago_service.AMOUNT_TOLERANCE:
        raise HTTPException(409, "El monto de la reserva cambió. Recarga la página e inténtalo de nuevo.")
    return float(expected)


@router.get("/mercadopago/public-key")
def get_mercadopago_public_key():
    return {
        "public_key": settings.MERCADO_PAGO_PUBLIC_KEY.strip()
    }


@router.post("/mercadopago/preference", response_model=MercadoPagoPreferenceResponse)
@limiter.limit(settings.RATE_LIMIT_PAYMENT)
def create_mercadopago_preference(
    payload: MercadoPagoPreferenceRequest,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Crea una preferencia de pago en Mercado Pago (Checkout Pro) para una reserva.
    Cobro único del 100 % de la reserva.
    Si se incluye signature_image_url, valida y registra la firma del contrato.
    """
    booking_id = parse_uuid(payload.booking_id, "booking_id")
    booking = db.get(Booking, booking_id, with_for_update=True)
    if not booking:
        raise HTTPException(404, detail="Reserva no encontrada")
    assert_booking_contractor_owner(db, booking, current_user)
    if booking.musician_id:
        db.query(MusicianProfile).filter(
            MusicianProfile.id == booking.musician_id
        ).with_for_update().first()

    in_flight_candidates = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.payment_type.is_distinct_from("refund"),
            Payment.status == PaymentStatus.initiated,
            Payment.gateway_payment_id.isnot(None),
        )
        .all()
    )
    existing_in_flight = next(
        (
            p
            for p in in_flight_candidates
            if not (isinstance(p.gateway_metadata, dict) and p.gateway_metadata.get("needs_refund"))
        ),
        None,
    )
    if existing_in_flight:
        raise HTTPException(
            409,
            detail="Ya hay un pago en proceso para esta reserva. Espera la confirmación antes de volver a intentar.",
        )

    # Validar el monto y el estado ANTES de mutar/firmar el contrato (BUG-17)
    total_amount = _server_amount(db, booking, payload.payment_type, payload.amount)

    # Si se envía firma del contrato durante la confirmación inicial
    if payload.signature_image_url and booking.status in (
        BookingStatus.accepted,
        BookingStatus.contract_pending,
        BookingStatus.contract_signed,
    ):
        if not payload.contract_body_sha256 or not payload.contract_body_sha256.strip():
            raise HTTPException(
                422,
                detail="Debes incluir contract_body_sha256 para verificar el texto firmado.",
            )
        sig_url = payload.signature_image_url
        if sig_url.startswith("data:image/"):
            from app.services.uploads import save_data_url_upload

            try:
                sig_url = save_data_url_upload(
                    sig_url,
                    private=True,
                    owner_id=current_user.id,
                    db=db,
                )
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
        else:
            assert_private_upload_owned(db, sig_url, current_user.id, "archivo de firma")
        from app.api.v1.endpoints.contracts import _ensure_booking_contract
        from app.services.contract_pdf import compute_contract_body_sha256

        contract = _ensure_booking_contract(db, booking, commit=False)
        if contract:
            current_sha = compute_contract_body_sha256(contract.title, contract.body)
            if payload.contract_body_sha256.strip() != current_sha:
                raise HTTPException(
                    409,
                    detail="El contenido del contrato cambió desde que lo abriste. Recarga la página antes de firmar.",
                )
            now = datetime.utcnow()
            real_ip = get_client_ip(request) or payload.sign_ip or "client"
            contract.terms_accepted = True
            contract.terms_accepted_at = now
            contract.terms_accepted_ip = real_ip
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = now
            contract.contractor_sign_ip = real_ip
            contract.contractor_signature_url = sig_url
            ctx = dict(contract.context or {})
            ctx["__signed_body_sha256__"] = current_sha
            contract.context = ctx

            musician = db.query(MusicianProfile).filter(
                MusicianProfile.id == booking.musician_id
            ).first()
            if musician and not contract.musician_signature_url and musician.signature_image_url:
                contract.musician_signature_url = musician.signature_image_url

            if booking.status in (BookingStatus.accepted, BookingStatus.contract_pending):
                booking.status = BookingStatus.contract_signed

            db.flush()

    if booking.status not in (BookingStatus.contract_signed, BookingStatus.payment_pending):
        from app.models.contract import Contract

        contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
        if not contract or not contract.contractor_signed:
            raise HTTPException(400, "Debes firmar el contrato antes de realizar el pago")

    try:
        pref = mercadopago_service.create_preference(
            db=db,
            booking=booking,
            contractor_user=current_user,
            amount=total_amount,
            payment_type=payload.payment_type,
            payer_email=payload.payer_email,
        )
        return MercadoPagoPreferenceResponse(**pref)
    except ValueError as exc:
        db.rollback()
        logger.warning("Error creando preferencia de Mercado Pago para reserva %s: %s", booking.id, exc)
        raise HTTPException(
            400,
            detail="No se pudo generar la preferencia de pago en este momento. Verifica los datos e inténtalo nuevamente.",
        ) from exc


@router.post("/mercadopago/process", response_model=MercadoPagoProcessPaymentResponse)
@limiter.limit(settings.RATE_LIMIT_PAYMENT)
def process_mercadopago_direct_payment(
    payload: MercadoPagoProcessPaymentRequest,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Procesa un pago directo usando el Checkout API de Mercado Pago (Yape nativo o Tarjeta vía Bricks/SDK).
    Ejecuta el cargo en tiempo real contra /v1/payments y confirma la reserva si es aprobado.
    """
    booking_id = parse_uuid(payload.booking_id, "booking_id")
    booking = db.get(Booking, booking_id, with_for_update=True)
    if not booking:
        raise HTTPException(404, detail="Reserva no encontrada")
    assert_booking_contractor_owner(db, booking, current_user)
    if booking.musician_id:
        db.query(MusicianProfile).filter(
            MusicianProfile.id == booking.musician_id
        ).with_for_update().first()

    in_flight_candidates = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.payment_type.is_distinct_from("refund"),
            Payment.status == PaymentStatus.initiated,
            Payment.gateway_payment_id.isnot(None),
        )
        .all()
    )
    existing_in_flight = next(
        (
            p
            for p in in_flight_candidates
            if not (isinstance(p.gateway_metadata, dict) and p.gateway_metadata.get("needs_refund"))
        ),
        None,
    )
    if existing_in_flight:
        raise HTTPException(
            409,
            detail="Ya hay un pago en proceso para esta reserva. Espera la confirmación antes de volver a intentar.",
        )

    # Validar el monto y el estado ANTES de mutar/firmar el contrato (BUG-17)
    total_amount = _server_amount(db, booking, payload.payment_type, payload.amount)

    # Si se envía firma del contrato durante la confirmación inicial
    if payload.signature_image_url and booking.status in (
        BookingStatus.accepted,
        BookingStatus.contract_pending,
        BookingStatus.contract_signed,
    ):
        if not payload.contract_body_sha256 or not payload.contract_body_sha256.strip():
            raise HTTPException(
                422,
                detail="Debes incluir contract_body_sha256 para verificar el texto firmado.",
            )
        sig_url = payload.signature_image_url
        if sig_url.startswith("data:image/"):
            from app.services.uploads import save_data_url_upload

            try:
                sig_url = save_data_url_upload(
                    sig_url,
                    private=True,
                    owner_id=current_user.id,
                    db=db,
                )
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
        else:
            assert_private_upload_owned(db, sig_url, current_user.id, "archivo de firma")
        from app.api.v1.endpoints.contracts import _ensure_booking_contract
        from app.services.contract_pdf import compute_contract_body_sha256

        contract = _ensure_booking_contract(db, booking, commit=False)
        if contract:
            current_sha = compute_contract_body_sha256(contract.title, contract.body)
            if payload.contract_body_sha256.strip() != current_sha:
                raise HTTPException(
                    409,
                    detail="El contenido del contrato cambió desde que lo abriste. Recarga la página antes de firmar.",
                )
            now = datetime.utcnow()
            real_ip = get_client_ip(request) or payload.sign_ip or "client"
            contract.terms_accepted = True
            contract.terms_accepted_at = now
            contract.terms_accepted_ip = real_ip
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = now
            contract.contractor_sign_ip = real_ip
            contract.contractor_signature_url = sig_url
            ctx = dict(contract.context or {})
            ctx["__signed_body_sha256__"] = current_sha
            contract.context = ctx

            musician = db.query(MusicianProfile).filter(
                MusicianProfile.id == booking.musician_id
            ).first()
            if musician and not contract.musician_signature_url and musician.signature_image_url:
                contract.musician_signature_url = musician.signature_image_url

            if booking.status in (BookingStatus.accepted, BookingStatus.contract_pending):
                booking.status = BookingStatus.contract_signed

            db.flush()

    if booking.status not in (BookingStatus.contract_signed, BookingStatus.payment_pending):
        from app.models.contract import Contract

        contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
        if not contract or not contract.contractor_signed:
            raise HTTPException(400, "Debes firmar el contrato antes de realizar el pago")

    payment_data = payload.model_dump()
    payment_data["amount"] = total_amount

    try:
        res = mercadopago_service.process_direct_payment(
            db=db,
            booking=booking,
            contractor_user=current_user,
            payment_payload=payment_data,
        )
        return MercadoPagoProcessPaymentResponse(
            success=res.get("success", False),
            status=res.get("status", "rejected"),
            status_detail=res.get("status_detail"),
            payment_id=str(res.get("payment_id")) if res.get("payment_id") else None,
            message=res.get("message", "Operación procesada"),
        )
    except ValueError as exc:
        db.rollback()
        logger.warning("Error procesando pago directo de Mercado Pago para reserva %s: %s", booking.id, exc)
        msg = str(exc)
        safe_detail = (
            msg
            if any(
                token in msg.lower()
                for token in ("teléfono", "otp", "token", "monto", "moneda", "no está configurad")
            )
            else "No se pudo procesar el pago con la pasarela. Verifica los datos ingresados e inténtalo nuevamente."
        )
        raise HTTPException(400, detail=safe_detail) from exc


@router.post("/mercadopago/webhook")
def mercadopago_webhook(
    request: Request,
    body_payload: dict[str, Any] | None = Body(default=None),
    db: Session = Depends(deps.get_db),
):
    """
    Webhook público para notificaciones instantáneas de pago (IPN / Webhooks) de Mercado Pago.
    Valida la firma criptográfica HMAC (si está configurada), consulta el API de Mercado Pago
    y actualiza la reserva y el pago a 'retained'.
    """
    # Mercado Pago puede enviar parámetros por querystring o en el body JSON
    query_params = dict(request.query_params)
    data_id = query_params.get("data.id") or query_params.get("id")
    topic = query_params.get("topic") or query_params.get("type")

    body_data = body_payload if isinstance(body_payload, dict) else {}

    if not data_id:
        data_id = (body_data.get("data") or {}).get("id") or body_data.get("id")
    if not topic:
        topic = body_data.get("type") or body_data.get("action")

    if not data_id:
        return {"status": "ignored", "reason": "no_data_id"}

    # Validación de firma criptográfica HMAC-SHA256 siempre (en producción rechaza si falta secreto)
    x_signature = request.headers.get("x-signature")
    x_request_id = request.headers.get("x-request-id")
    if not mercadopago_service.verify_webhook_signature(
        x_signature=x_signature,
        x_request_id=x_request_id,
        data_id=str(data_id),
    ):
        logger.warning("Firma inválida o ausente en webhook de Mercado Pago (id: %s)", data_id)
        return {"status": "ignored", "reason": "invalid_signature"}

    # Manejo de notificaciones de merchant_order
    if topic in ("merchant_order", "merchant_orders"):
        try:
            order_details = mercadopago_service.get_merchant_order_details(str(data_id))
            payments = order_details.get("payments") or []
            processed_count = 0
            for p in payments:
                pid = str(p.get("id"))
                if pid:
                    p_details = mercadopago_service.get_payment_details(pid)
                    if p_details:
                        mercadopago_service.process_approved_mercadopago_payment(db, p_details)
                        processed_count += 1
            return {"status": "ok", "merchant_order_id": str(data_id), "payments_processed": processed_count}
        except Exception as exc:
            logger.error("Error al procesar merchant_order %s: %s", data_id, exc)
            raise HTTPException(500, detail="Error procesando notificación de orden") from exc

    # Notificaciones estándar de pago (topic == "payment" o action "payment.*")
    payment_id = str(data_id)
    try:
        payment_details = mercadopago_service.get_payment_details(payment_id)
        if payment_details:
            mercadopago_service.process_approved_mercadopago_payment(db, payment_details)
            return {"status": "ok", "payment_id": payment_id, "mp_status": payment_details.get("status")}
    except Exception as exc:
        logger.error("Error en webhook de Mercado Pago para pago %s: %s", payment_id, exc)
        raise HTTPException(500, detail="Error procesando notificación de pago") from exc

    return {"status": "ok", "payment_id": payment_id}


@router.get("/mercadopago/check-status/{booking_id}", response_model=MercadoPagoPaymentCheckResponse)
def check_mercadopago_payment_status(
    booking_id: str,
    payment_id: str | None = Query(None),
    collection_id: str | None = Query(None),
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Verifica de manera síncrona el estado de pago de una reserva en Mercado Pago.
    Utilizado cuando el contratista retorna al frontend con ?mp_status=approved&payment_id=... o ?collection_id=...
    """
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    effective_payment_id = payment_id or collection_id
    recent_payment: Payment | None = None
    if not effective_payment_id:
        # Fallback: buscar el pago más reciente asociado a esta reserva que posea gateway_payment_id
        recent_payment = (
            db.query(Payment)
            .filter(
                Payment.booking_id == booking.id,
                Payment.payment_type.is_distinct_from("refund"),
                Payment.gateway_payment_id.isnot(None),
            )
            .order_by(Payment.created_at.desc())
            .first()
        )
        if recent_payment and recent_payment.gateway_payment_id:
            effective_payment_id = recent_payment.gateway_payment_id

    mp_status: str | None = None
    if effective_payment_id:
        effective_payment_id = str(effective_payment_id).strip()
        if not re.fullmatch(r"\d{1,20}", effective_payment_id):
            raise HTTPException(400, "Identificador de pago inválido.")
        try:
            details = mercadopago_service.get_payment_details(effective_payment_id)
            if details:
                ext_ref = (details.get("metadata") or {}).get("booking_id") or details.get("external_reference")
                if ext_ref and str(ext_ref) != str(booking.id):
                    raise HTTPException(400, "El pago indicado no corresponde a esta reserva.")
                mp_status = details.get("status")
                mercadopago_service.process_approved_mercadopago_payment(db, details)
                db.refresh(booking)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("No se pudo sincronizar pago %s: %s", effective_payment_id, exc)
            if recent_payment is not None:
                if recent_payment.status == PaymentStatus.rejected:
                    mp_status = "rejected"
                elif recent_payment.status == PaymentStatus.initiated:
                    mp_status = "pending"

    is_approved = booking.status in (
        BookingStatus.payment_retained,
        BookingStatus.in_progress,
        BookingStatus.payment_released,
        BookingStatus.completed,
    )
    is_rejected = not is_approved and (
        mp_status in ("rejected", "cancelled")
        or (mp_status is None and recent_payment is not None and recent_payment.status == PaymentStatus.rejected)
    )
    has_payment_in_flight = bool(effective_payment_id) or booking.status == BookingStatus.payment_pending

    if is_approved:
        status_value = "approved"
        message = "¡Tu pago fue verificado y retenido con éxito! La reserva está confirmada."
    elif is_rejected:
        status_value = "rejected"
        message = "Mercado Pago rechazó el pago. Puedes intentarlo nuevamente."
    elif has_payment_in_flight:
        status_value = "pending"
        message = "El pago se encuentra en proceso de validación por Mercado Pago."
    else:
        status_value = "not_started"
        message = "Aún no se ha iniciado ningún pago para esta reserva."

    return MercadoPagoPaymentCheckResponse(
        status=status_value,
        payment_id=effective_payment_id,
        booking_status=booking.status.value,
        is_approved=is_approved,
        message=message,
    )


from app.models.payout_request import PayoutRequest, PayoutRequestBooking
from app.schemas.payment import PayoutRequestOut


@router.post("/musician/request-payout", response_model=PayoutRequestOut)
def request_payout(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo los músicos pueden solicitar retiro")

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.user_id == current_user.id)
        .first()
    )
    if not musician:
        raise HTTPException(400, "Completa tu perfil de músico primero")
    if not current_user.is_verified:
        raise HTTPException(403, "Tu perfil de músico debe estar verificado para solicitar retiros")

    # Orden canónico de candados: Booking -> MusicianProfile (SEC4-12)
    db.query(Booking.id).filter(
        Booking.musician_id == musician.id,
        Booking.status.in_(
            [
                BookingStatus.payment_retained,
                BookingStatus.in_progress,
                BookingStatus.payment_released,
                BookingStatus.completed,
                BookingStatus.cancelled,
            ]
        ),
    ).order_by(Booking.id.asc()).with_for_update().all()

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == musician.id)
        .with_for_update()
        .first()
    )

    summary = get_musician_earnings(current_user=current_user, db=db)

    eligible_debts = [
        d
        for d in summary.debts
        if d.debt_state == "payable"
        and d.booking_status == BookingStatus.completed.value
        and d.retained_total > 0
    ]
    amount_dec = sum(
        (Decimal(str(d.retained_total)) for d in eligible_debts),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))
    amount = float(amount_dec)

    if amount <= 0 or not eligible_debts:
        awaiting_reviews = [
            d for d in summary.debts if d.debt_state == "awaiting_reviews" and d.retained_total > 0
        ]
        if awaiting_reviews:
            raise HTTPException(
                400,
                "Aún no tienes reservas listas para desembolso. Verifica que el evento haya culminado y que tanto el contratista como tú hayan dejado su reseña.",
            )
        raise HTTPException(
            400,
            "No tienes saldo disponible para retirar. Solo se pueden desembolsar reservas culminadas donde tanto el contratista como tú hayan dejado su reseña.",
        )

    # Persistir el paso a 'completed' de las reservas elegibles que ya cuentan con ambas reseñas (SEC2-05)
    eligible_ids = [d.booking_id for d in eligible_debts]
    if eligible_ids:
        from app.services.booking_share import disable_booking_share

        bookings_to_promote = (
            db.query(Booking)
            .filter(
                Booking.id.in_(eligible_ids),
                Booking.status.in_(
                    [
                        BookingStatus.in_progress,
                        BookingStatus.payment_retained,
                        BookingStatus.payment_released,
                    ]
                ),
            )
            .all()
        )
        for b_row in bookings_to_promote:
            b_row.status = BookingStatus.completed
            disable_booking_share(b_row)

    booking_ids_str = ", ".join(str(d.booking_id) for d in eligible_debts)
    notes_text = f"Reservas culminadas con ambas reseñas: {booking_ids_str}"

    existing_request = (
        db.query(PayoutRequest)
        .filter(
            PayoutRequest.musician_id == musician.id,
            PayoutRequest.status == "pending",
        )
        .order_by(PayoutRequest.created_at.desc())
        .with_for_update()
        .first()
    )
    if existing_request:
        existing_request.amount = amount
        existing_request.notes = notes_text
        payout_request = existing_request
        db.flush()
        db.query(PayoutRequestBooking).filter(
            PayoutRequestBooking.payout_request_id == payout_request.id
        ).delete(synchronize_session=False)
    else:
        payout_request = PayoutRequest(
            musician_id=musician.id,
            amount=amount,
            status="pending",
            notes=notes_text,
        )
        db.add(payout_request)
        db.flush()

    for b_id in eligible_ids:
        db.add(
            PayoutRequestBooking(
                payout_request_id=payout_request.id,
                booking_id=b_id,
            )
        )

    try:
        notify_admins(
            db,
            type="payout_requested",
            title="Nueva solicitud de retiro de músico",
            message=(
                f"{musician.stage_name or current_user.fullname or 'Un músico'} solicitó un retiro "
                f"por S/ {amount:.2f} ({len(eligible_debts)} reserva(s) culminadas)."
            ),
            meta={"musician_id": str(musician.id), "amount": amount},
        )
    except Exception as exc:
        logger.warning("No se pudo notificar a los administradores sobre solicitud de retiro: %s", exc)

    db.commit()
    db.refresh(payout_request)
    return payout_request

