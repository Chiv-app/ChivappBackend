import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.config import settings

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
from app.services.booking_notifications import notify_payment_released
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
    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo los músicos pueden ver sus ingresos")

    from app.models.booking_complaint import BookingComplaint
    from app.services.platform_payment import musician_portion_of_paid
    from app.services.settlement import settlement_state_for_booking

    musician = get_musician_profile_or_400(db, current_user, require_verified=True)
    bookings = (
        db.query(Booking)
        .filter(Booking.musician_id == musician.id)
        .order_by(Booking.event_date.desc())
        .all()
    )

    total_quoted = 0.0
    bookings_active = 0
    bookings_completed = 0
    bookings_cancelled = 0
    booking_by_id = {}

    for booking in bookings:
        booking_by_id[booking.id] = booking
        if booking.status == BookingStatus.cancelled:
            bookings_cancelled += 1
            continue
        if booking.status == BookingStatus.completed:
            bookings_completed += 1
        elif booking.status not in {BookingStatus.requested}:
            bookings_active += 1
        if booking.price_agreed is not None:
            total_quoted += float(booking.price_agreed)

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

    total_released = 0.0
    total_retained = 0.0
    total_pending = 0.0
    total_app_debt = 0.0
    total_disputed = 0.0
    total_retained_active = 0.0
    items: list[MusicianEarningsItem] = []
    debts: list[MusicianDebtItem] = []

    for payment in payments:
        booking = booking_by_id.get(payment.booking_id)
        if not booking:
            continue
        amount = float(
            musician_portion_of_paid(booking, Decimal(str(payment.amount)))
        )
        if payment.status == PaymentStatus.released:
            total_released += amount
        elif payment.status == PaymentStatus.retained:
            total_retained += amount
            if booking.status != BookingStatus.completed:
                total_retained_active += amount
        elif payment.status == PaymentStatus.initiated:
            total_pending += amount

        items.append(
            MusicianEarningsItem(
                payment_id=payment.id,
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                booking_status=booking.status.value,
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
        if booking.status == BookingStatus.cancelled:
            continue
        booking_payments = payments_by_booking.get(booking.id, [])
        retained_gross = sum(
            float(p.amount)
            for p in booking_payments
            if p.status == PaymentStatus.retained
        )
        released_gross = sum(
            float(p.amount)
            for p in booking_payments
            if p.status == PaymentStatus.released
        )
        retained = float(
            musician_portion_of_paid(booking, Decimal(str(retained_gross)))
        )
        released = float(
            musician_portion_of_paid(booking, Decimal(str(released_gross)))
        )
        if retained <= 0 and released <= 0 and booking.status != BookingStatus.completed:
            continue

        complaint = complaint_by_booking.get(booking.id)
        state = settlement_state_for_booking(booking, complaint, retained, released)
        if state == "none":
            continue

        if state == "payable":
            total_app_debt += retained
        elif state in {"disputed", "awaiting_admin"}:
            total_disputed += retained

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
                booking_status=booking.status.value,
                debt_state=state,
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

    return MusicianEarningsSummary(
        currency="PEN",
        total_quoted=total_quoted,
        total_released=total_released,
        total_retained=total_retained,
        total_pending=total_pending,
        total_app_debt=total_app_debt,
        total_disputed=total_disputed,
        total_retained_active=total_retained_active,
        bookings_active=bookings_active,
        bookings_completed=bookings_completed,
        bookings_cancelled=bookings_cancelled,
        items=items,
        debts=debts,
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

    payment.status = PaymentStatus.released
    payment.released_at = datetime.utcnow()
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
    "full": (BookingStatus.contract_pending, BookingStatus.contract_signed, BookingStatus.payment_pending),
}


def _server_amount(db: Session, booking: Booking, payment_type: str, client_amount) -> float:
    """Calcula el monto en el servidor. El monto del cliente solo se usa para
    detectar que la pantalla quedó desactualizada, nunca para cobrar."""
    allowed = PAYABLE_STATUSES.get(payment_type)
    if allowed is None:
        raise HTTPException(400, "Tipo de pago no soportado")
    if booking.status not in allowed:
        raise HTTPException(400, "La reserva no está en un estado que permita este pago")
    try:
        expected = mercadopago_service.expected_payment_amount(db, booking, payment_type)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
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
def create_mercadopago_preference(
    payload: MercadoPagoPreferenceRequest,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Crea una preferencia de pago en Mercado Pago (Checkout Pro) para una reserva.
    Cobro único del 100 % de la reserva.
    Si se incluye signature_image_url, valida y registra la firma del contrato.
    """
    booking = get_booking_or_404(db, payload.booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    # Si se envía firma del contrato durante la confirmación inicial
    if payload.signature_image_url and booking.status == BookingStatus.contract_pending:
        assert_private_upload_owned(db, payload.signature_image_url, current_user.id, "archivo de firma")
        contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
        if contract:
            now = datetime.utcnow()
            contract.terms_accepted = True
            contract.terms_accepted_at = now
            contract.terms_accepted_ip = payload.sign_ip or "client"
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = now
            contract.contractor_sign_ip = payload.sign_ip or "client"
            contract.contractor_signature_url = payload.signature_image_url

            musician = db.query(MusicianProfile).filter(
                MusicianProfile.id == booking.musician_id
            ).first()
            if musician and not contract.musician_signature_url and musician.signature_image_url:
                contract.musician_signature_url = musician.signature_image_url

            if booking.status == BookingStatus.contract_pending:
                booking.status = BookingStatus.contract_signed

            db.commit()

    total_amount = _server_amount(db, booking, payload.payment_type, payload.amount)

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
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/mercadopago/process", response_model=MercadoPagoProcessPaymentResponse)
def process_mercadopago_direct_payment(
    payload: MercadoPagoProcessPaymentRequest,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Procesa un pago directo usando el Checkout API de Mercado Pago (Yape nativo o Tarjeta vía Bricks/SDK).
    Ejecuta el cargo en tiempo real contra /v1/payments y confirma la reserva si es aprobado.
    """
    booking = get_booking_or_404(db, payload.booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    # Si se envía firma del contrato durante la confirmación inicial
    if payload.signature_image_url and booking.status == BookingStatus.contract_pending:
        assert_private_upload_owned(db, payload.signature_image_url, current_user.id, "archivo de firma")
        contract = db.query(Contract).filter(Contract.booking_id == booking.id).first()
        if contract:
            now = datetime.utcnow()
            contract.terms_accepted = True
            contract.terms_accepted_at = now
            contract.terms_accepted_ip = payload.sign_ip or "client"
            contract.contractor_signed = True
            contract.contractor_sign_timestamp = now
            contract.contractor_sign_ip = payload.sign_ip or "client"
            contract.contractor_signature_url = payload.signature_image_url

            musician = db.query(MusicianProfile).filter(
                MusicianProfile.id == booking.musician_id
            ).first()
            if musician and not contract.musician_signature_url and musician.signature_image_url:
                contract.musician_signature_url = musician.signature_image_url

            if booking.status == BookingStatus.contract_pending:
                booking.status = BookingStatus.contract_signed

            db.commit()

    total_amount = _server_amount(db, booking, payload.payment_type, payload.amount)

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
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/mercadopago/webhook")
async def mercadopago_webhook(
    request: Request,
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

    body_data = {}
    try:
        body_data = await request.json()
    except Exception:
        body_data = {}

    if not data_id:
        data_id = (body_data.get("data") or {}).get("id") or body_data.get("id")
    if not topic:
        topic = body_data.get("type") or body_data.get("action")

    # Validación de firma criptográfica HMAC-SHA256 si hay secreto configurado
    x_signature = request.headers.get("x-signature")
    x_request_id = request.headers.get("x-request-id")
    if settings.MERCADO_PAGO_WEBHOOK_SECRET and data_id:
        if not mercadopago_service.verify_webhook_signature(
            x_signature=x_signature,
            x_request_id=x_request_id,
            data_id=str(data_id),
        ):
            logger.warning("Firma inválida o ausente en webhook de Mercado Pago (id: %s)", data_id)
            return {"status": "ignored", "reason": "invalid_signature"}

    if not data_id:
        return {"status": "ignored", "reason": "no_data_id"}

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
            return {"status": "error_logged", "order_id": str(data_id), "detail": str(exc)}

    # Notificaciones estándar de pago (topic == "payment" o action "payment.*")
    payment_id = str(data_id)
    try:
        payment_details = mercadopago_service.get_payment_details(payment_id)
        if payment_details:
            mercadopago_service.process_approved_mercadopago_payment(db, payment_details)
            return {"status": "ok", "payment_id": payment_id, "mp_status": payment_details.get("status")}
    except Exception as exc:
        # Retornamos 200 para evitar que Mercado Pago reintente indefinidamente en caso de pagos no encontrados
        logger.error("Error en webhook de Mercado Pago para pago %s: %s", payment_id, exc)
        return {"status": "error_logged", "payment_id": payment_id, "detail": str(exc)}

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
    if not effective_payment_id:
        # Fallback: buscar el pago más reciente asociado a esta reserva que posea gateway_payment_id
        recent_payment = (
            db.query(Payment)
            .filter(Payment.booking_id == booking.id)
            .order_by(Payment.created_at.desc())
            .first()
        )
        if recent_payment and recent_payment.gateway_payment_id:
            effective_payment_id = recent_payment.gateway_payment_id

    mp_status: str | None = None
    if effective_payment_id:
        try:
            details = mercadopago_service.get_payment_details(effective_payment_id)
            if details:
                mp_status = details.get("status")
                mercadopago_service.process_approved_mercadopago_payment(db, details)
                db.refresh(booking)
        except Exception as exc:
            logger.warning("No se pudo sincronizar pago %s: %s", effective_payment_id, exc)

    is_approved = booking.status in (
        BookingStatus.payment_retained,
        BookingStatus.in_progress,
        BookingStatus.payment_released,
        BookingStatus.completed,
    )
    is_rejected = not is_approved and mp_status in ("rejected", "cancelled")

    if is_approved:
        status_value = "approved"
        message = "¡Tu pago fue verificado y retenido con éxito! La reserva está confirmada."
    elif is_rejected:
        status_value = "rejected"
        message = "Mercado Pago rechazó el pago. Puedes intentarlo nuevamente."
    else:
        status_value = "pending"
        message = "El pago se encuentra en proceso de validación por Mercado Pago."

    return MercadoPagoPaymentCheckResponse(
        status=status_value,
        payment_id=effective_payment_id,
        booking_status=booking.status.value,
        is_approved=is_approved,
        message=message,
    )

