import hashlib
import hmac
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.models.musician_profile import MusicianProfile
from app.services.booking_notifications import notify_booking_confirmed, notify_balance_submitted

logger = logging.getLogger(__name__)

MERCADO_PAGO_API_BASE = "https://api.mercadopago.com"


def _mp_headers() -> dict[str, str]:
    token = settings.MERCADO_PAGO_ACCESS_TOKEN.strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def is_mercadopago_configured() -> bool:
    return bool(settings.MERCADO_PAGO_ACCESS_TOKEN and settings.MERCADO_PAGO_PUBLIC_KEY)


def get_webhook_url() -> str:
    if settings.MERCADO_PAGO_WEBHOOK_BASE_URL:
        base = settings.MERCADO_PAGO_WEBHOOK_BASE_URL.rstrip("/")
    elif settings.OAUTH_REDIRECT_BASE_URL:
        base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    else:
        base = "http://localhost:8000/api/v1"
    if "/api/v1" in base:
        return f"{base}/payments/mercadopago/webhook"
    return f"{base}/api/v1/payments/mercadopago/webhook"


def create_preference(
    db: Session,
    booking: Booking,
    contractor_user: User,
    amount: float,
    payment_type: str,
    payer_email: str | None = None,
) -> dict[str, Any]:
    if not is_mercadopago_configured():
        raise ValueError("Mercado Pago no estÃ¡ configurado en el servidor.")

    musician = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
    musician_name = musician.stage_name if musician and musician.stage_name else "MÃºsico"

    type_labels = {
        "advance": "Anticipo",
        "full": "Pago Total",
        "balance": "Saldo Final",
    }
    type_label = type_labels.get(payment_type, "Pago")

    item_title = f"Chivapp: {booking.event_type} - {musician_name}"
    item_description = f"Reserva #{str(booking.id)[:8]} ({type_label})"

    frontend_base = settings.FRONTEND_URL.rstrip("/")
    success_url = f"{frontend_base}/contractor/bookings/{booking.id}?mp_status=approved"
    pending_url = f"{frontend_base}/contractor/bookings/{booking.id}?mp_status=pending"
    failure_url = f"{frontend_base}/contractor/bookings/{booking.id}?mp_status=failure"

    # Effective payer email resolution:
    effective_email = (payer_email or "").strip() or contractor_user.email
    # Only in sandbox mode fallback to test buyer to avoid Mercado Pago self-payment rejection
    if settings.MERCADO_PAGO_SANDBOX:
        if "@testuser.com" not in effective_email:
            if settings.MERCADO_PAGO_ACCESS_TOKEN.startswith("TEST-") or "3671163102" in settings.MERCADO_PAGO_ACCESS_TOKEN:
                effective_email = "test@testuser.com"

    payload: dict[str, Any] = {
        "items": [
            {
                "id": f"booking-{booking.id}-{payment_type}",
                "title": item_title,
                "description": item_description,
                "quantity": 1,
                "unit_price": round(float(amount), 2),
                "currency_id": "PEN",
            }
        ],
        "payer": {
            "email": effective_email,
            "name": contractor_user.fullname or "Cliente Chivapp",
        },
        "external_reference": str(booking.id),
        "metadata": {
            "booking_id": str(booking.id),
            "payment_type": payment_type,
            "amount": str(round(float(amount), 2)),
            "contractor_id": str(booking.contractor_id),
        },
        "notification_url": get_webhook_url(),
        "statement_descriptor": "CHIVAPP",
    }

    # Mercado Pago strictly requires HTTPS for back_urls when auto_return is enabled.
    # In local HTTP development (e.g. http://localhost:3000), sending auto_return causes a 400 error.
    if frontend_base.startswith("https://"):
        payload["back_urls"] = {
            "success": success_url,
            "pending": pending_url,
            "failure": failure_url,
        }
        payload["auto_return"] = "approved"
    else:
        logger.info(
            "Frontend base URL is HTTP (%s). Omitting auto_return to prevent Mercado Pago API validation errors in local development.",
            frontend_base,
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{MERCADO_PAGO_API_BASE}/checkout/preferences",
                headers=_mp_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "preference_id": data.get("id"),
                "init_point": data.get("init_point"),
                "sandbox_init_point": data.get("sandbox_init_point") if settings.MERCADO_PAGO_SANDBOX else None,
                "public_key": settings.MERCADO_PAGO_PUBLIC_KEY,
                "amount": round(float(amount), 2),
                "currency": "PEN",
                "payment_type": payment_type,
            }
    except httpx.HTTPStatusError as exc:
        logger.error("Error al crear preferencia en Mercado Pago: %s %s", exc.response.status_code, exc.response.text)
        raise ValueError(f"Mercado Pago error ({exc.response.status_code}): {exc.response.text}") from exc
    except Exception as exc:
        logger.error("Error de conexiÃ³n con Mercado Pago: %s", exc)
        raise ValueError(f"Error al conectar con Mercado Pago: {exc}") from exc


def get_payment_details(payment_id: str) -> dict[str, Any]:
    if not is_mercadopago_configured():
        raise ValueError("Mercado Pago no estÃ¡ configurado.")

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{MERCADO_PAGO_API_BASE}/v1/payments/{payment_id}",
                headers=_mp_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Error al obtener pago %s de Mercado Pago: %s", payment_id, exc.response.text)
        raise ValueError(f"Error al consultar pago en Mercado Pago: {exc.response.text}") from exc
    except Exception as exc:
        logger.error("Error de conexiÃ³n con Mercado Pago para pago %s: %s", payment_id, exc)
        raise ValueError(f"Error al conectar con Mercado Pago: {exc}") from exc


def get_merchant_order_details(merchant_order_id: str) -> dict[str, Any]:
    if not is_mercadopago_configured():
        raise ValueError("Mercado Pago no estÃ¡ configurado.")

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{MERCADO_PAGO_API_BASE}/merchant_orders/{merchant_order_id}",
                headers=_mp_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Error al obtener merchant order %s de Mercado Pago: %s", merchant_order_id, exc.response.text)
        raise ValueError(f"Error al consultar orden en Mercado Pago: {exc.response.text}") from exc
    except Exception as exc:
        logger.error("Error de conexiÃ³n con Mercado Pago para merchant order %s: %s", merchant_order_id, exc)
        raise ValueError(f"Error al conectar con Mercado Pago: {exc}") from exc


def verify_webhook_signature(
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
) -> bool:
    secret = settings.MERCADO_PAGO_WEBHOOK_SECRET.strip()
    if not secret:
        return True

    if not x_signature or not data_id:
        return False

    try:
        parts = dict(part.split("=", 1) for part in x_signature.split(",") if "=" in part)
        ts = parts.get("ts")
        v1 = parts.get("v1")
        if not ts or not v1:
            return False

        template = f"id:{data_id};request-id:{x_request_id or ''};ts:{ts};"
        computed = hmac.new(secret.encode(), template.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, v1)
    except Exception:
        return False


def process_approved_mercadopago_payment(db: Session, payment_data: dict[str, Any]) -> Payment | None:
    payment_id = str(payment_data.get("id"))
    status = payment_data.get("status")
    metadata = payment_data.get("metadata") or {}
    booking_id_str = metadata.get("booking_id") or payment_data.get("external_reference")

    if not booking_id_str:
        logger.warning("Pago %s de Mercado Pago no contiene booking_id ni external_reference", payment_id)
        return None

    from app.api.booking_helpers import parse_uuid
    try:
        booking_uuid = parse_uuid(booking_id_str, "booking_id")
    except Exception:
        logger.warning("Booking ID invÃ¡lido en pago %s: %s", payment_id, booking_id_str)
        return None

    booking = db.get(Booking, booking_uuid)
    if not booking:
        logger.warning("Reserva no encontrada para pago %s: %s", payment_id, booking_id_str)
        return None

    existing = db.query(Payment).filter(Payment.gateway_payment_id == payment_id).first()
    if existing:
        if status == "approved" and existing.status != PaymentStatus.retained:
            existing.status = PaymentStatus.retained
            existing.retained_at = existing.retained_at or datetime.utcnow()
            existing.gateway_metadata = payment_data
            if booking.status in (BookingStatus.contract_signed, BookingStatus.payment_pending):
                booking.status = BookingStatus.payment_retained
            db.commit()
            db.refresh(existing)
        return existing

    transaction_amount = payment_data.get("transaction_amount")
    amount = Decimal(str(transaction_amount)) if transaction_amount is not None else Decimal("0")
    payment_type = metadata.get("payment_type") or "advance"

    is_approved = (status == "approved")
    new_payment_status = PaymentStatus.retained if is_approved else PaymentStatus.initiated

    active_payment = (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id, Payment.status == PaymentStatus.initiated)
        .order_by(Payment.created_at.desc())
        .first()
    )

    if active_payment and not active_payment.gateway_payment_id:
        payment = active_payment
        payment.amount = amount
        payment.payment_type = payment_type
        payment.gateway_provider = "mercadopago"
        payment.gateway_payment_id = payment_id
        payment.gateway_preference_id = payment_data.get("preference_id")
        payment.gateway_metadata = payment_data
        payment.status = new_payment_status
        if is_approved:
            payment.retained_at = datetime.utcnow()
    else:
        payment = Payment(
            booking_id=booking.id,
            amount=amount,
            currency=payment_data.get("currency_id") or "PEN",
            payment_type=payment_type,
            status=new_payment_status,
            gateway_provider="mercadopago",
            gateway_payment_id=payment_id,
            gateway_preference_id=payment_data.get("preference_id"),
            gateway_metadata=payment_data,
            retained_at=datetime.utcnow() if is_approved else None,
        )
        db.add(payment)

    if is_approved:
        if payment_type in ("advance", "full"):
            booking.status = BookingStatus.payment_retained
            
            contractor = db.query(ContractorProfile).filter(ContractorProfile.id == booking.contractor_id).first()
            contractor_user = db.query(User).filter(User.id == contractor.user_id).first() if contractor else None

            musician = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
            if musician:
                musician_user = db.query(User).filter(User.id == musician.user_id).first()
                if musician_user:
                    notify_booking_confirmed(db, musician_user, str(booking.id), contractor_user)
                    
                    # Create Google Calendar event
                    if not booking.calendar_event_id and contractor_user and musician_user:
                        from app.services.calendar_service import create_booking_event
                        try:
                            # Estimate duration (default 1 hour if not specified)
                            duration_hours = 1
                            if booking.start_time and booking.end_time:
                                import datetime as dt
                                start_dt = dt.datetime.combine(booking.event_date, booking.start_time)
                                end_dt = dt.datetime.combine(booking.event_date, booking.end_time)
                                if end_dt > start_dt:
                                    duration_hours = (end_dt - start_dt).seconds // 3600
                            
                            event_date_time = dt.datetime.combine(booking.event_date, booking.start_time)
                            
                            event_id = create_booking_event(
                                booking_id=str(booking.id),
                                event_type=booking.event_type,
                                event_date=event_date_time,
                                duration_hours=duration_hours or 1,
                                location_city=booking.location_city or "",
                                location_zone=booking.location_reference or "",
                                location_address=booking.location_address or "",
                                musician_email=musician_user.email,
                                contractor_email=contractor_user.email,
                                contractor_name=contractor_user.fullname or "Contratista",
                                musician_name=musician.stage_name,
                                musician_phone=musician_user.phone or "",
                                contractor_phone=contractor_user.phone or ""
                            )
                            if event_id:
                                booking.calendar_event_id = event_id
                        except Exception as e:
                            logger.error(f"Error calling create_booking_event: {e}")
                            
        elif payment_type == "balance":
            musician = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
            if musician:
                musician_user = db.query(User).filter(User.id == musician.user_id).first()
                if musician_user:
                    notify_balance_submitted(db, musician_user, str(booking.id))

    db.commit()
    db.refresh(payment)
    return payment


def process_direct_payment(
    db: Session,
    booking: Booking,
    contractor_user: User,
    payment_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Procesa un pago directo (Checkout API / Payments API) usando un token generado en el frontend.
    Soporta Yape nativo (token vÃ­a mp.yape) y Tarjetas de crÃ©dito/dÃ©bito (token vÃ­a Bricks/SDK).
    """
    if not is_mercadopago_configured():
        raise ValueError("Mercado Pago no estÃ¡ configurado en el servidor.")

    token = payment_payload.get("token")
    if not token:
        raise ValueError("El token de pago generado por Mercado Pago es obligatorio.")

    payment_method_id = payment_payload.get("payment_method_id", "yape")
    payment_type = payment_payload.get("payment_type", "advance")
    amount = float(payment_payload.get("amount") or payment_payload.get("transaction_amount") or 0.0)
    if amount <= 0:
        raise ValueError("El monto a pagar debe ser mayor a 0.")

    installments = int(payment_payload.get("installments", 1))
    issuer_id = payment_payload.get("issuer_id")

    musician = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
    musician_name = musician.stage_name if musician and musician.stage_name else "MÃºsico"
    description = f"Chivapp: {booking.event_type} - {musician_name} (Reserva #{str(booking.id)[:8]})"

    payer_email = payment_payload.get("payer_email") or contractor_user.email
    if settings.MERCADO_PAGO_SANDBOX:
        if "@testuser.com" not in (payer_email or ""):
            if settings.MERCADO_PAGO_ACCESS_TOKEN.startswith("TEST-") or "3671163102" in settings.MERCADO_PAGO_ACCESS_TOKEN:
                payer_email = "test@testuser.com"

    payer_data: dict[str, Any] = {
        "email": payer_email,
        "first_name": contractor_user.fullname or "Cliente",
    }

    id_type = payment_payload.get("identification_type")
    id_num = payment_payload.get("identification_number")
    if id_type and id_num:
        payer_data["identification"] = {
            "type": id_type,
            "number": id_num,
        }

    mp_body: dict[str, Any] = {
        "token": token,
        "transaction_amount": float(round(amount, 2)),
        "description": description,
        "payment_method_id": payment_method_id,
        "installments": installments,
        "payer": payer_data,
        "external_reference": str(booking.id),
        "metadata": {
            "booking_id": str(booking.id),
            "payment_type": payment_type,
            "amount": str(round(amount, 2)),
            "contractor_id": str(booking.contractor_id),
            "payment_method": payment_method_id,
        },
        "notification_url": get_webhook_url(),
        "statement_descriptor": "CHIVAPP",
    }

    if issuer_id:
        mp_body["issuer_id"] = issuer_id

    headers = _mp_headers()
    headers["X-Idempotency-Key"] = str(uuid.uuid4())
    device_id = payment_payload.get("device_id")
    if device_id:
        headers["X-meli-session-id"] = device_id

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{MERCADO_PAGO_API_BASE}/v1/payments",
                headers=headers,
                json=mp_body,
            )
            data = resp.json()
            if resp.status_code >= 400:
                safe_body = {k: v for k, v in mp_body.items() if k != "token"}
                logger.error(
                    "Error al procesar pago directo en Mercado Pago: %s %s - Body: %s",
                    resp.status_code,
                    resp.text,
                    safe_body,
                )
                message = data.get("message") or "El pago no pudo ser procesado."
                cause = data.get("cause")
                if cause and isinstance(cause, list) and len(cause) > 0:
                    first_cause = cause[0]
                    desc = first_cause.get("description")
                    if desc:
                        message = f"{message}: {desc}"
                return {
                    "success": False,
                    "status": "rejected",
                    "status_detail": data.get("error") or "api_error",
                    "payment_id": None,
                    "message": message,
                }

            status = data.get("status")
            status_detail = data.get("status_detail")
            payment_id = str(data.get("id")) if data.get("id") else None

            detail_messages = {
                "rejected_high_risk": "Pago rechazado por prevenciÃ³n de riesgo/fraude de Mercado Pago. (Nota: en modo producciÃ³n no puedes pagarte a ti mismo con tu propio celular/tarjeta/cuenta de Mercado Pago).",
                "rejected_by_bank": "El banco emisor o la billetera rechazÃ³ la operaciÃ³n.",
                "cc_rejected_insufficient_amount": "Saldo o fondos insuficientes en la cuenta.",
                "cc_rejected_bad_filled_security_code": "CÃ³digo de seguridad o validaciÃ³n invÃ¡lido.",
                "cc_rejected_bad_filled_date": "Fecha de vencimiento de la tarjeta invÃ¡lida.",
                "cc_rejected_bad_filled_other": "Datos ingresados incorrectos o no coinciden.",
                "cc_rejected_call_for_authorize": "Debes llamar a tu banco para autorizar compras por internet.",
                "cc_rejected_card_disabled": "Tu tarjeta o cuenta no estÃ¡ habilitada para pagos por internet.",
                "cc_rejected_duplicated_payment": "Se detectÃ³ un pago duplicado reciente para este monto.",
                "cc_rejected_max_attempts": "Se superÃ³ el lÃ­mite de intentos permitidos. Intenta mÃ¡s tarde.",
            }

            if status == "approved":
                friendly_message = "Â¡Pago aprobado con Ã©xito! Tu reserva ha quedado confirmada."
            elif status == "in_process":
                friendly_message = "El pago estÃ¡ en proceso de revisiÃ³n por la entidad financiera."
            elif status == "pending":
                friendly_message = "El pago se encuentra pendiente de acreditaciÃ³n."
            else:
                friendly_message = (
                    detail_messages.get(status_detail)
                    or f"El pago fue rechazado ({status_detail or 'motivo desconocido'}). Verifica los datos o intenta con otro medio de pago."
                )

            if status == "approved":
                process_approved_mercadopago_payment(db, data)

            return {
                "success": (status == "approved"),
                "status": status,
                "status_detail": status_detail,
                "payment_id": payment_id,
                "message": friendly_message,
                "raw": data,
            }

    except Exception as exc:
        logger.error("ExcepciÃ³n al contactar con Mercado Pago v1/payments: %s", exc)
        return {
            "success": False,
            "status": "error",
            "status_detail": "network_error",
            "payment_id": None,
            "message": f"Error de comunicaciÃ³n con la pasarela de pago: {exc}",
        }

def issue_refund(payment_id: str, amount: float) -> dict[str, Any]:
    """
    Emite un reembolso total o parcial para un pago procesado.
    Llama a: POST /v1/payments/{payment_id}/refunds
    """
    if not is_mercadopago_configured():
        raise ValueError("Mercado Pago no estÃ¡ configurado.")

    payload = {"amount": float(amount)}
    headers = _mp_headers()
    headers["X-Idempotency-Key"] = f"refund_{payment_id}_{amount}_{uuid.uuid4()}"

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{MERCADO_PAGO_API_BASE}/v1/payments/{payment_id}/refunds",
                headers=headers,
                json=payload,
            )
            data = resp.json()
            if resp.status_code >= 400:
                logger.error("Error al reembolsar en Mercado Pago: %s %s", resp.status_code, resp.text)
                message = data.get("message") or "Error en el reembolso."
                raise ValueError(f"Fallo en Mercado Pago ({resp.status_code}): {message}")
            return data
    except httpx.HTTPStatusError as exc:
        logger.error("Error HTTP al reembolsar: %s", exc)
        raise ValueError(f"Error HTTP: {exc}")
    except Exception as exc:
        logger.error("ExcepciÃ³n en reembolso: %s", exc)
        raise ValueError(f"ExcepciÃ³n al reembolsar: {exc}")
