"""Integration tests for Mercado Pago flows using httpx.MockTransport (Fase 6)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import date, time as dt_time, timedelta
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.platform_payment import contractor_payable_total


def _auth_cookies(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def _sign_webhook(secret: str, data_id: str, request_id: str, ts: int | None = None) -> str:
    ts_val = ts if ts is not None else int(time.time())
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts_val};"
    digest = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"ts={ts_val},v1={digest}"


def _create_signed_booking(db: Session, *, price: Decimal = Decimal("400.00")) -> tuple[User, User, Booking]:
    suffix = uuid.uuid4().hex[:8]
    digits = str(uuid.uuid4().int)[:8]
    musician_user = User(
        email=f"mp_mus_{suffix}@test.com",
        password_hash=hash_password("TestPass123!"),
        fullname="Mariachi MP",
        phone=f"+519{digits}",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    contractor_user = User(
        email=f"mp_con_{suffix}@test.com",
        password_hash=hash_password("TestPass123!"),
        fullname="Contratista MP",
        phone=f"+518{digits}",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
    )
    db.add_all([musician_user, contractor_user])
    db.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name=f"Mariachi MP {suffix}",
        price_per_event=price,
        status=ProfileStatus.published,
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        city="Lima",
        status=ProfileStatus.published,
    )
    db.add_all([musician, contractor])
    db.flush()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_type="Serenata",
        event_date=date.today() + timedelta(days=10),
        start_time=dt_time(19, 0),
        end_time=dt_time(20, 0),
        location_city="Lima",
        location_address="Av. Arequipa 1200",
        price_agreed=price,
        status=BookingStatus.contract_signed,
    )
    db.add(booking)
    db.flush()

    contract = Contract(
        booking_id=booking.id,
        title="Contrato de Prestación Musical",
        body="Cláusula única de prueba.",
        contractor_signed=True,
        terms_accepted=True,
    )
    db.add(contract)
    db.commit()
    db.refresh(booking)
    return musician_user, contractor_user, booking


def _patch_mp_transport(monkeypatch, handler):
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-MP-PUB-123")
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.services.mercadopago_service.httpx.Client", _client_factory)


def test_mock_transport_create_preference(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verifica create_preference con MockTransport: payload https vs http y error 500 -> 400."""
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    _, contractor_user, booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))

    captured_requests: list[tuple[httpx.Request, dict]] = []
    should_fail = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        captured_requests.append((request, body))
        if should_fail["flag"]:
            return httpx.Response(500, json={"message": "Internal MP error"})
        return httpx.Response(
            201,
            json={
                "id": "pref-mock-001",
                "init_point": "https://www.mercadopago.com.pe/checkout/v1/redirect?pref_id=pref-mock-001",
                "sandbox_init_point": "https://sandbox.mercadopago.com.pe/checkout/v1/redirect?pref_id=pref-mock-001",
            },
        )

    _patch_mp_transport(monkeypatch, handler)

    # 1. Con FRONTEND_URL https:// incluye back_urls y auto_return="approved"
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://chivapp.pe")
    res_https = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "amount": payable,
        },
        headers=_auth_cookies(contractor_user),
    )
    assert res_https.status_code == 200, res_https.text
    assert res_https.json()["preference_id"] == "pref-mock-001"
    req_https, body_https = captured_requests[-1]
    assert req_https.headers.get("X-Idempotency-Key", "").startswith(f"pref-{booking.id}-full-")
    assert body_https["external_reference"] == str(booking.id)
    assert body_https["items"][0]["unit_price"] == payable
    assert body_https["auto_return"] == "approved"
    assert body_https["back_urls"]["success"].startswith("https://chivapp.pe/contractor/bookings")

    # 2. Con FRONTEND_URL http:// omite auto_return y back_urls
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:3000")
    res_http = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "amount": payable,
        },
        headers=_auth_cookies(contractor_user),
    )
    assert res_http.status_code == 200, res_http.text
    _, body_http = captured_requests[-1]
    assert "auto_return" not in body_http
    assert "back_urls" not in body_http

    # 3. Si Mercado Pago responde 500, el endpoint devuelve 400 amigable
    should_fail["flag"] = True
    res_err = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "amount": payable,
        },
        headers=_auth_cookies(contractor_user),
    )
    assert res_err.status_code == 400


def test_mock_transport_process_direct_payment_approved_rejected_pending(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verifica /mercadopago/process para pago aprobado, rechazado y pendiente + bloqueo 409 de segundo intento."""
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    post_calls: list[httpx.Request] = []
    next_response: dict = {"status": "approved", "status_detail": "accredited", "id": 900001}

    def handler(request: httpx.Request) -> httpx.Response:
        post_calls.append(request)
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            201,
            json={
                "id": next_response["id"],
                "status": next_response["status"],
                "status_detail": next_response["status_detail"],
                "transaction_amount": body["transaction_amount"],
                "currency_id": "PEN",
                "external_reference": body["external_reference"],
                "metadata": body.get("metadata") or {},
            },
        )

    _patch_mp_transport(monkeypatch, handler)

    # Caso A: Aprobado -> booking.status == payment_retained, Payment.status == retained
    _, contractor_a, booking_a = _create_signed_booking(db_session)
    payable_a = float(contractor_payable_total(booking_a))
    res_ok = client.post(
        "/api/v1/payments/mercadopago/process",
        json={
            "booking_id": str(booking_a.id),
            "payment_type": "full",
            "payment_method_id": "visa",
            "token": "tok_visa_approved_123",
            "amount": payable_a,
        },
        headers=_auth_cookies(contractor_a),
    )
    assert res_ok.status_code == 200, res_ok.text
    assert res_ok.json()["success"] is True
    assert res_ok.json()["status"] == "approved"
    assert post_calls[-1].headers.get("X-Idempotency-Key", "").startswith("pay-")
    db_session.refresh(booking_a)
    assert booking_a.status == BookingStatus.payment_retained
    pay_a = db_session.query(Payment).filter(Payment.booking_id == booking_a.id).one()
    assert pay_a.status == PaymentStatus.retained

    # Caso B: Rechazado -> Payment.status == rejected y mensaje en español
    _, contractor_b, booking_b = _create_signed_booking(db_session)
    payable_b = float(contractor_payable_total(booking_b))
    next_response.update({"id": 900002, "status": "rejected", "status_detail": "cc_rejected_insufficient_amount"})
    res_rej = client.post(
        "/api/v1/payments/mercadopago/process",
        json={
            "booking_id": str(booking_b.id),
            "payment_type": "full",
            "payment_method_id": "visa",
            "token": "tok_visa_rejected_123",
            "amount": payable_b,
        },
        headers=_auth_cookies(contractor_b),
    )
    assert res_rej.status_code == 200, res_rej.text
    assert res_rej.json()["success"] is False
    assert res_rej.json()["status"] == "rejected"
    assert "fondos insuficientes" in res_rej.json()["message"].lower()
    pay_b = db_session.query(Payment).filter(Payment.booking_id == booking_b.id).one()
    assert pay_b.status == PaymentStatus.rejected

    # Caso C: Pendiente (in_process) -> Payment.status == initiated; segundo intento recibe 409 sin llamar a MP
    _, contractor_c, booking_c = _create_signed_booking(db_session)
    payable_c = float(contractor_payable_total(booking_c))
    next_response.update({"id": 900003, "status": "in_process", "status_detail": "pending_review_manual"})
    calls_before = len(post_calls)
    res_pend = client.post(
        "/api/v1/payments/mercadopago/process",
        json={
            "booking_id": str(booking_c.id),
            "payment_type": "full",
            "payment_method_id": "visa",
            "token": "tok_visa_pending_123",
            "amount": payable_c,
        },
        headers=_auth_cookies(contractor_c),
    )
    assert res_pend.status_code == 200, res_pend.text
    assert res_pend.json()["status"] == "in_process"
    assert len(post_calls) == calls_before + 1

    # Segundo POST mientras sigue initiated con gateway_payment_id -> 409 sin nuevo POST a MP
    res_dup = client.post(
        "/api/v1/payments/mercadopago/process",
        json={
            "booking_id": str(booking_c.id),
            "payment_type": "full",
            "payment_method_id": "visa",
            "token": "tok_visa_pending_456",
            "amount": payable_c,
        },
        headers=_auth_cookies(contractor_c),
    )
    assert res_dup.status_code == 409
    assert len(post_calls) == calls_before + 1


def test_mock_transport_webhook_signature_replay_and_merchant_order(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verifica webhook de Mercado Pago: firma válida, inválida, ts expirado, replay idempotente y merchant_order."""
    secret = "whsec_test_mp_secret_999"
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_WEBHOOK_SECRET", secret)

    _, _, booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))

    # Pre-creamos un pago pendiente (initiated) para probar transición pending -> approved
    pending_payment = Payment(
        booking_id=booking.id,
        amount=Decimal(str(payable)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.initiated,
        gateway_provider="mercadopago",
        gateway_payment_id="777001",
    )
    db_session.add(pending_payment)
    db_session.commit()

    get_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        get_calls.append(str(request.url))
        if "/merchant_orders/555001" in str(request.url):
            return httpx.Response(
                200,
                json={"id": 555001, "payments": [{"id": 777002, "status": "approved"}]},
            )
        if "/v1/payments/777001" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": 777001,
                    "status": "approved",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": str(booking.id),
                    "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
                },
            )
        if "/v1/payments/777002" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": 777002,
                    "status": "approved",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": str(booking_mo.id),
                    "metadata": {"booking_id": str(booking_mo.id), "payment_type": "full"},
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    _, _, booking_mo = _create_signed_booking(db_session)
    _patch_mp_transport(monkeypatch, handler)

    # 1. Firma inválida -> ignored sin llamar a GET /v1/payments
    res_bad = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": "ts=1700000000,v1=deadbeef", "x-request-id": "req-1"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_bad.status_code == 200
    assert res_bad.json()["status"] == "ignored"
    assert len(get_calls) == 0

    # 2. Timestamp expirado (> 300s) -> ignored sin llamar a GET /v1/payments
    old_sig = _sign_webhook(secret, "777001", "req-old", ts=int(time.time()) - 900)
    res_old = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": old_sig, "x-request-id": "req-old"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_old.status_code == 200
    assert res_old.json()["reason"] == "invalid_signature"
    assert len(get_calls) == 0

    # 3. Firma válida -> promueve el pago initiated a retained y la reserva a payment_retained
    valid_sig = _sign_webhook(secret, "777001", "req-valid")
    res_ok = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": valid_sig, "x-request-id": "req-valid"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "ok"
    db_session.refresh(booking)
    db_session.refresh(pending_payment)
    assert booking.status == BookingStatus.payment_retained
    assert pending_payment.status == PaymentStatus.retained

    # 4. Replay del mismo webhook -> idempotente, sigue existiendo 1 solo Payment para booking
    res_replay = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": valid_sig, "x-request-id": "req-valid"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_replay.status_code == 200
    assert db_session.query(Payment).filter(Payment.booking_id == booking.id).count() == 1

    # 5. Notificación merchant_order -> consulta /merchant_orders/555001 y /v1/payments/777002
    mo_sig = _sign_webhook(secret, "555001", "req-mo")
    res_mo = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=555001&topic=merchant_order",
        headers={"x-signature": mo_sig, "x-request-id": "req-mo"},
        json={"id": "555001", "topic": "merchant_order"},
    )
    assert res_mo.status_code == 200
    assert res_mo.json()["payments_processed"] == 1
    db_session.refresh(booking_mo)
    assert booking_mo.status == BookingStatus.payment_retained


def test_mock_transport_webhook_post_approval_events(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verifica eventos post-aprobación en webhook: in_mediation, partial refund y charged_back."""
    secret = "whsec_test_post_approval"
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_WEBHOOK_SECRET", secret)

    _, _, booking = _create_signed_booking(db_session)
    booking.status = BookingStatus.payment_retained
    payable = float(contractor_payable_total(booking))
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal(str(payable)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        gateway_provider="mercadopago",
        gateway_payment_id="888001",
    )
    db_session.add(payment)
    db_session.commit()

    mp_state = {"status": "in_mediation", "refunded": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 888001,
                "status": mp_state["status"],
                "transaction_amount": payable,
                "transaction_amount_refunded": mp_state["refunded"],
                "currency_id": "PEN",
                "external_reference": str(booking.id),
                "metadata": {"booking_id": str(booking.id)},
            },
        )

    _patch_mp_transport(monkeypatch, handler)
    sig = _sign_webhook(secret, "888001", "req-post")

    # 1. in_mediation -> conserva retained pero registra alerta post_approval_event = in_mediation
    res_med = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=888001&type=payment",
        headers={"x-signature": sig, "x-request-id": "req-post"},
        json={"data": {"id": "888001"}, "type": "payment"},
    )
    assert res_med.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.retained
    assert payment.gateway_metadata["post_approval_event"] == "in_mediation"
    assert payment.gateway_metadata["in_mediation"] is True

    # 2. Reembolso parcial en MP -> alerta partial_refund sin cancelar la reserva
    mp_state.update({"status": "approved", "refunded": 50.0})
    res_part = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=888001&type=payment",
        headers={"x-signature": sig, "x-request-id": "req-post"},
        json={"data": {"id": "888001"}, "type": "payment"},
    )
    assert res_part.status_code == 200
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.gateway_metadata["post_approval_event"] == "partial_refund"
    assert payment.gateway_metadata["partial_refund_amount"] == 50.0
    assert booking.status == BookingStatus.payment_retained

    # 3. Contracargo (charged_back) -> pasa el pago a PaymentStatus.refunded y cancela la reserva
    mp_state.update({"status": "charged_back", "refunded": payable})
    res_cb = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=888001&type=payment",
        headers={"x-signature": sig, "x-request-id": "req-post"},
        json={"data": {"id": "888001"}, "type": "payment"},
    )
    assert res_cb.status_code == 200
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.status == PaymentStatus.refunded
    assert payment.gateway_metadata["post_approval_event"] == "charged_back"
    assert booking.status == BookingStatus.cancelled


def test_mock_transport_issue_refund_and_check_status(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verifica reembolso HTTP con X-Idempotency-Key y sincronización /check-status/{booking_id}."""
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    _, contractor_user, booking = _create_signed_booking(db_session)
    _, _, other_booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))

    admin = User(
        email=f"admin_mp_{uuid.uuid4().hex[:6]}@test.com",
        password_hash=hash_password("AdminPass123!"),
        fullname="Admin MP",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    flagged_payment = Payment(
        booking_id=booking.id,
        amount=Decimal(str(payable)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.initiated,
        gateway_provider="mercadopago",
        gateway_payment_id="666001",
        gateway_metadata={"needs_refund": True, "reason": "late_webhook"},
    )
    db_session.add_all([admin, flagged_payment])
    db_session.commit()
    db_session.refresh(flagged_payment)

    refund_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/v1/payments/666001/refunds" in str(request.url):
            refund_headers.append(dict(request.headers))
            return httpx.Response(201, json={"id": 123456, "status": "approved"})
        if "/v1/payments/666002" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": 666002,
                    "status": "approved",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": str(booking.id),
                    "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
                },
            )
        if "/v1/payments/666999" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": 666999,
                    "status": "approved",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": str(other_booking.id),
                    "metadata": {"booking_id": str(other_booking.id), "payment_type": "full"},
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    _patch_mp_transport(monkeypatch, handler)

    # 1. Admin reembolsa pago needs_refund -> envía X-Idempotency-Key: needs-refund-{payment.id}
    res_ref = client.post(
        f"/api/v1/admin/payments/{flagged_payment.id}/refund",
        headers=_auth_cookies(admin),
    )
    assert res_ref.status_code == 200, res_ref.text
    assert refund_headers[-1].get("x-idempotency-key") == f"needs-refund-{flagged_payment.id}"
    db_session.refresh(flagged_payment)
    assert flagged_payment.status == PaymentStatus.refunded

    # 2. check-status rechaza un payment_id cuyo external_reference pertenece a otra reserva
    res_mismatch = client.get(
        f"/api/v1/payments/mercadopago/check-status/{booking.id}?payment_id=666999",
        headers=_auth_cookies(contractor_user),
    )
    assert res_mismatch.status_code == 400

    # 3. check-status sincroniza un pago aprobado en MP y confirma la reserva
    res_check = client.get(
        f"/api/v1/payments/mercadopago/check-status/{booking.id}?payment_id=666002",
        headers=_auth_cookies(contractor_user),
    )
    assert res_check.status_code == 200, res_check.text
    assert res_check.json()["is_approved"] is True
    db_session.refresh(booking)
    assert booking.status == BookingStatus.payment_retained


def test_mock_transport_concurrent_two_threads_double_click(
    db_session: Session, monkeypatch
) -> None:
    """Verifica con 2 sesiones independientes en 2 hilos que un doble clic en /mercadopago/process solo ejecuta 1 POST a MP."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.api.v1.endpoints.payments import process_mercadopago_direct_payment
    from app.schemas.payment import MercadoPagoProcessPaymentRequest
    from tests.conftest import TestingSessionLocal

    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-MP-PUBKEY-123")

    _, contractor_user, booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))
    booking_id_str = str(booking.id)
    user_id = contractor_user.id

    mp_post_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/v1/payments" in str(request.url):
            mp_post_calls.append({"headers": dict(request.headers)})
            return httpx.Response(
                201,
                json={
                    "id": 999111,
                    "status": "approved",
                    "status_detail": "accredited",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": booking_id_str,
                    "metadata": {"booking_id": booking_id_str, "payment_type": "full"},
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    _patch_mp_transport(monkeypatch, handler)
    barrier = threading.Barrier(2)

    def _worker(idx: int) -> tuple[str, int | str]:
        session = TestingSessionLocal()
        try:
            u = session.get(User, user_id)
            req = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/payments/mercadopago/process",
                    "headers": [],
                    "client": ("127.0.0.1", 50000 + idx),
                }
            )
            payload = MercadoPagoProcessPaymentRequest(
                booking_id=booking_id_str,
                payment_type="full",
                payment_method_id="visa",
                token=f"tok_concurrent_{idx}",
                amount=payable,
            )
            barrier.wait(timeout=5)
            res = process_mercadopago_direct_payment(
                payload=payload,
                request=req,
                current_user=u,
                db=session,
            )
            return ("ok", res.status)
        except HTTPException as exc:
            session.rollback()
            return ("http_error", exc.status_code)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker, 1), pool.submit(_worker, 2)]
        outcomes = [f.result() for f in futures]

    # Exactamente 1 hilo debe procesar el cargo en MP y el otro debe ser rechazado (400/409) tras liberar el lock
    assert outcomes.count(("ok", "approved")) == 1, f"Resultados inesperados: {outcomes}"
    assert any(kind == "http_error" and code in (400, 409) for kind, code in outcomes), outcomes
    assert len(mp_post_calls) == 1
    db_session.expire_all()
    assert db_session.query(Payment).filter(Payment.booking_id == booking.id).count() == 1

