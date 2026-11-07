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


def _auth_cookie(client: TestClient, user: User) -> None:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    client.cookies.set("access_token", token)


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

    # 1. Firma inválida -> 401 sin llamar a GET /v1/payments y alerta a admin (máx. 1 por hora)
    admin_user = User(
        email="admin.wh.alert@test.com",
        fullname="Admin Webhook",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin_user)
    db_session.commit()

    res_bad = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": "ts=1700000000,v1=deadbeef", "x-request-id": "req-1"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_bad.status_code == 401
    assert res_bad.json()["detail"]["reason"] == "invalid_signature"
    assert len(get_calls) == 0

    from app.models.notification import Notification

    alerts_after_first = (
        db_session.query(Notification)
        .filter(Notification.type == "webhook_security_alert")
        .count()
    )
    assert alerts_after_first >= 1

    # 2. Timestamp expirado (> 24h) -> 401 sin llamar a GET /v1/payments y sin duplicar alerta en la misma hora
    old_sig = _sign_webhook(secret, "777001", "req-old", ts=int(time.time()) - 90000)
    res_old = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=777001&type=payment",
        headers={"x-signature": old_sig, "x-request-id": "req-old"},
        json={"data": {"id": "777001"}, "type": "payment"},
    )
    assert res_old.status_code == 401
    assert res_old.json()["detail"]["reason"] == "invalid_signature"
    assert len(get_calls) == 0
    alerts_after_second = (
        db_session.query(Notification)
        .filter(Notification.type == "webhook_security_alert")
        .count()
    )
    assert alerts_after_second == alerts_after_first

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

    # 1b. Mediación resuelta a favor del vendedor (status vuelve a approved sin reembolso) -> limpia in_mediation
    mp_state.update({"status": "approved", "refunded": 0.0})
    res_med_resolved = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=888001&type=payment",
        headers={"x-signature": sig, "x-request-id": "req-post"},
        json={"data": {"id": "888001"}, "type": "payment"},
    )
    assert res_med_resolved.status_code == 200
    db_session.refresh(payment)
    assert payment.gateway_metadata["in_mediation"] is False
    assert payment.gateway_metadata["post_approval_event"] == "mediation_resolved"

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


def test_mock_transport_manual_refund_never_calls_mp_and_check_status(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Mercado Pago solo cobra: el reembolso de un pago marcado es manual (no llama a /refunds),
    y /check-status/{booking_id} sigue sincronizando pagos aprobados."""
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

    # 1. Admin registra el reembolso manual del pago needs_refund: exige la referencia y no toca MP
    res_sin_ref = client.post(
        f"/api/v1/admin/payments/{flagged_payment.id}/refund",
        headers=_auth_cookies(admin),
    )
    assert res_sin_ref.status_code == 400
    res_ref = client.post(
        f"/api/v1/admin/payments/{flagged_payment.id}/refund",
        headers=_auth_cookies(admin),
        json={"manual_reason": "Transferencia OP-666001"},
    )
    assert res_ref.status_code == 200, res_ref.text
    assert refund_headers == []  # ninguna llamada a /refunds de Mercado Pago
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


def test_webhook_unknown_topic_and_manual_payment_verification(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """
    Sin cron ni conciliación automática (todo es manual para el admin):
    1) Webhook con topic desconocido ('claims') responde 200 ignored.
    2) Si Mercado Pago está caído, 'Verificar pago' responde 502 y no cambia la reserva.
    3) Con Mercado Pago operativo, 'Verificar pago' registra el pago aprobado y confirma la reserva.
    4) Repetirlo no duplica el pago (idempotente) y responde que ya estaba confirmado.
    5) Una reserva sin pagos en Mercado Pago responde no_payment_found.
    6) El admin ve las reservas vencidas con el filtro `stale` (sin respuesta, sin pago, pago sin confirmar).
    """
    from datetime import datetime

    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-MP-PUBKEY-123")

    admin = User(
        email="admin.verify@test.com",
        fullname="Admin Verify",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin)
    db_session.commit()

    # 1. Webhook con tipo desconocido -> 200 ignored
    res_claim = client.post(
        "/api/v1/payments/mercadopago/webhook?topic=claims&id=abc-claim-1",
        json={"type": "claims", "data": {"id": "abc-claim-1"}},
    )
    assert res_claim.status_code == 200
    assert res_claim.json()["status"] == "ignored"
    assert res_claim.json()["reason"] == "unsupported_topic"

    _, _, booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))
    _, _, booking_without_payment = _create_signed_booking(db_session)

    mp = {"outage": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if mp["outage"]:
            return httpx.Response(503, json={"message": "service unavailable"})
        if "/v1/payments/search" in str(request.url):
            if request.url.params.get("external_reference") == str(booking.id):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": 910001,
                                "status": "approved",
                                "status_detail": "accredited",
                                "transaction_amount": payable,
                                "currency_id": "PEN",
                                "external_reference": str(booking.id),
                                "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
                            }
                        ]
                    },
                )
        return httpx.Response(200, json={"results": []})

    _patch_mp_transport(monkeypatch, handler)
    _auth_cookie(client, admin)

    # 2. Mercado Pago caído
    res_outage = client.post(f"/api/v1/admin/bookings/{booking.id}/verify-payment")
    assert res_outage.status_code == 502, res_outage.text
    db_session.refresh(booking)
    assert booking.status == BookingStatus.contract_signed

    # 3. Mercado Pago operativo: se registra y confirma
    mp["outage"] = False
    res_ok = client.post(f"/api/v1/admin/bookings/{booking.id}/verify-payment")
    assert res_ok.status_code == 200, res_ok.text
    assert res_ok.json()["result"] == "confirmed"
    assert res_ok.json()["payments_found"] == 1
    db_session.expire_all()
    db_session.refresh(booking)
    assert booking.status == BookingStatus.payment_retained
    payments = db_session.query(Payment).filter(Payment.booking_id == booking.id).all()
    assert len(payments) == 1 and payments[0].status == PaymentStatus.retained

    # 4. Idempotente
    res_again = client.post(f"/api/v1/admin/bookings/{booking.id}/verify-payment")
    assert res_again.status_code == 200, res_again.text
    assert res_again.json()["result"] == "already_confirmed"
    assert db_session.query(Payment).filter(Payment.booking_id == booking.id).count() == 1

    # 5. Sin pagos en Mercado Pago
    res_none = client.post(f"/api/v1/admin/bookings/{booking_without_payment.id}/verify-payment")
    assert res_none.status_code == 200, res_none.text
    assert res_none.json()["result"] == "no_payment_found"

    # 6. Filtros de vencidas
    now = datetime.utcnow()
    musician_id = booking.musician_id
    contractor_id = booking.contractor_id

    def _booking(status: BookingStatus, **extra) -> Booking:
        row = Booking(
            musician_id=musician_id,
            contractor_id=contractor_id,
            event_type="Boda",
            event_date=date.today() + timedelta(days=40),
            start_time=dt_time(20, 0),
            location_city="Lima",
            location_address="Av. Prueba 123",
            price_agreed=Decimal("400.00"),
            status=status,
            **extra,
        )
        db_session.add(row)
        db_session.flush()
        return row

    requested_old = _booking(BookingStatus.requested, created_at=now - timedelta(hours=80))
    requested_new = _booking(BookingStatus.requested, created_at=now - timedelta(hours=2))
    unpaid_old = _booking(BookingStatus.accepted, quoted_at=now - timedelta(days=9), created_at=now - timedelta(days=9))
    unconfirmed = _booking(BookingStatus.contract_signed, created_at=now - timedelta(hours=3))
    db_session.add(
        Payment(
            booking_id=unconfirmed.id,
            amount=Decimal("420.00"),
            payment_type="full",
            status=PaymentStatus.initiated,
            created_at=now - timedelta(hours=1),
        )
    )
    db_session.commit()

    def _ids(stale: str) -> set[str]:
        res = client.get(f"/api/v1/admin/bookings?stale={stale}&limit=200")
        assert res.status_code == 200, res.text
        return {item["id"] for item in res.json()}

    assert str(requested_old.id) in _ids("requested_no_response")
    assert str(requested_new.id) not in _ids("requested_no_response")
    assert str(unpaid_old.id) in _ids("unpaid_expired")
    assert str(unconfirmed.id) in _ids("unconfirmed_payment")
    assert str(booking.id) not in _ids("unpaid_expired")  # ya pagada
    assert client.get("/api/v1/admin/bookings?stale=otra").status_code == 400


def test_wp02_wp03_safe_settlement_order_and_single_release_path(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """
    WP-02 & WP-03:
    1) Liquidación (`settle`) bloqueada por `in_mediation` devuelve 400 y no llama a Mercado Pago.
    2) `/payments/{id}/release` rechaza liberar si hay mediación activa o queja abierta (400).
    3) Cuando la disputa se resuelve a favor del vendedor (`status='approved'`), se desbloquea `settle`.
    4) La liquidación con devolución NO llama a Mercado Pago: la devolución queda pendiente de que el
       admin la transfiera a mano (`awaiting_transfer`) y el saldo del músico se libera.
    """
    from app.models.booking_complaint import (
        REFUND_STATUS_AWAITING_TRANSFER,
        BookingComplaint,
        BookingComplaintStatus,
    )

    secret = "whsec_wp02"
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-MP-PUBKEY-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_WEBHOOK_SECRET", secret)

    admin = User(
        email="admin.wp02@test.com",
        fullname="Admin WP02",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin)
    _, contractor_user, booking = _create_signed_booking(db_session)
    booking.status = BookingStatus.completed
    payable = float(contractor_payable_total(booking))
    tariff = float(booking.price_agreed)

    payment = Payment(
        booking_id=booking.id,
        amount=Decimal(str(payable)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        gateway_provider="mercadopago",
        gateway_payment_id="920001",
        gateway_metadata={"in_mediation": True, "post_approval_event": "in_mediation"},
    )
    db_session.add(payment)
    db_session.flush()

    complaint = BookingComplaint(
        booking_id=booking.id,
        opened_by_user_id=contractor_user.id,
        reason="El grupo llegó 30 minutos tarde",
        status=BookingComplaintStatus.musician_responded,
        musician_response="Hubo tráfico pero tocamos completo",
    )
    db_session.add(complaint)
    db_session.commit()

    refund_post_calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "POST" and "/refunds" in url_str:
            refund_post_calls.append({"url": url_str, "headers": dict(request.headers)})
            return httpx.Response(201, json={"id": 700111, "status": "approved", "amount": 100.0})
        if request.method == "GET" and "/v1/payments/920001" in url_str:
            return httpx.Response(
                200,
                json={
                    "id": 920001,
                    "status": "approved",
                    "transaction_amount": payable,
                    "transaction_amount_refunded": 100.0 if refund_post_calls else 0.0,
                    "refunds": [{"id": 700111, "status": "approved", "amount": 100.0}] if refund_post_calls else [],
                    "currency_id": "PEN",
                    "external_reference": str(booking.id),
                    "metadata": {"booking_id": str(booking.id)},
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    _patch_mp_transport(monkeypatch, handler)
    _auth_cookie(client, admin)

    # 1. Liquidación bloqueada por in_mediation -> 400 y CERO llamadas a /refunds
    res_blocked = client.post(
        f"/api/v1/admin/settlements/{booking.id}/settle",
        json={
            "musician_amount": round(tariff - 100.0, 2),
            "contractor_refund": 100.0,
            "notes": "Intento durante mediación",
        },
    )
    assert res_blocked.status_code == 400
    assert "mediación" in res_blocked.json()["detail"].lower()
    assert len(refund_post_calls) == 0

    # 2. WP-03: /payments/{id}/release también bloqueado por mediación y por queja abierta
    res_rel_blocked = client.post(f"/api/v1/payments/{payment.id}/release")
    assert res_rel_blocked.status_code == 400

    # 3. Webhook de MP resuelve la disputa a favor del vendedor (status='approved') -> limpia in_mediation
    sig = _sign_webhook(secret, "920001", "req-wp02-resolve")
    res_wh = client.post(
        "/api/v1/payments/mercadopago/webhook?data.id=920001&type=payment",
        headers={"x-signature": sig, "x-request-id": "req-wp02-resolve"},
        json={"data": {"id": "920001"}, "type": "payment"},
    )
    assert res_wh.status_code == 200
    db_session.refresh(payment)
    assert payment.gateway_metadata.get("in_mediation") is False

    # WP-03: Aun sin mediación, /payments/{id}/release sigue bloqueado mientras haya queja sin liquidar
    res_rel_complaint = client.post(f"/api/v1/payments/{payment.id}/release")
    assert res_rel_complaint.status_code == 400
    assert "reclamo" in res_rel_complaint.json()["detail"].lower()

    # 4. Liquidar con devolución: Mercado Pago NO se toca; la devolución queda pendiente de transferir
    res_settle = client.post(
        f"/api/v1/admin/settlements/{booking.id}/settle",
        json={
            "musician_amount": round(tariff - 100.0, 2),
            "contractor_refund": 100.0,
            "notes": "Acuerdo tras la mediación",
        },
    )
    assert res_settle.status_code == 200, res_settle.text
    assert len(refund_post_calls) == 0
    db_session.expire_all()
    db_session.refresh(complaint)
    db_session.refresh(payment)
    assert complaint.refund_status == REFUND_STATUS_AWAITING_TRANSFER
    assert complaint.status == BookingComplaintStatus.settled
    assert payment.status == PaymentStatus.released


def test_wp05_preference_initiated_row_duplicates_flagged_and_network_error(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """
    WP-05:
    1) Crear preferencia Checkout Pro registra una fila Payment en estado 'initiated' (y la reutiliza si se pide de nuevo).
    2) Si la llamada HTTP de /process falla por red (network_error), deja una fila 'initiated' con idempotency_key
       lista para ser conciliada por WP-01.
    3) Dos pagos aprobados para la misma reserva terminan con el primero en 'retained' y el segundo marcado
       `needs_refund` para que el admin lo devuelva a mano: Mercado Pago no reembolsa nunca por API.
    """
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-MP-TOKEN-123")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-MP-PUBKEY-123")

    _, contractor_user, booking = _create_signed_booking(db_session)
    payable = float(contractor_payable_total(booking))

    refund_calls: list[str] = []
    simulate_net_error = {"active": False}

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/checkout/preferences" in url_str:
            return httpx.Response(
                201,
                json={
                    "id": "pref-wp05-1",
                    "init_point": "https://www.mercadopago.com.pe/checkout/v1/redirect?pref_id=pref-wp05-1",
                },
            )
        if request.method == "POST" and "/v1/payments" in url_str and "/refunds" not in url_str:
            if simulate_net_error["active"]:
                raise httpx.ConnectError("Simulated connection reset")
            return httpx.Response(
                201,
                json={
                    "id": 950001,
                    "status": "approved",
                    "status_detail": "accredited",
                    "transaction_amount": payable,
                    "currency_id": "PEN",
                    "external_reference": str(booking.id),
                    "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
                },
            )
        if request.method == "POST" and "/refunds" in url_str:
            refund_calls.append(url_str)
            return httpx.Response(
                201,
                json={"id": 850001, "status": "approved", "amount": payable},
            )
        return httpx.Response(404, json={"message": "not found"})

    _patch_mp_transport(monkeypatch, handler)
    _auth_cookie(client, contractor_user)

    # 1. Crear preferencia -> crea 1 fila Payment en initiated y la reutiliza si se vuelve a llamar
    res_pref1 = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={"booking_id": str(booking.id), "payment_type": "full", "amount": payable},
    )
    assert res_pref1.status_code == 200, res_pref1.text
    res_pref2 = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={"booking_id": str(booking.id), "payment_type": "full", "amount": payable},
    )
    assert res_pref2.status_code == 200, res_pref2.text
    init_rows = db_session.query(Payment).filter(Payment.booking_id == booking.id).all()
    assert len(init_rows) == 1
    assert init_rows[0].status == PaymentStatus.initiated
    assert init_rows[0].gateway_preference_id == "pref-wp05-1"

    # 2. Error de red en /process -> deja la fila initiated con network_error=True e idempotency_key
    simulate_net_error["active"] = True
    res_net = client.post(
        "/api/v1/payments/mercadopago/process",
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "payment_method_id": "visa",
            "token": "tok_wp05_net",
            "amount": payable,
        },
    )
    assert res_net.status_code == 200
    assert res_net.json()["status"] == "error"
    assert res_net.json()["status_detail"] == "network_error"
    db_session.expire_all()
    net_row = db_session.query(Payment).filter(Payment.booking_id == booking.id).first()
    assert net_row is not None
    assert net_row.status == PaymentStatus.initiated
    assert net_row.gateway_metadata.get("network_error") is True
    assert net_row.gateway_metadata.get("idempotency_key", "").startswith("pay-")

    # 3. Dos pagos aprobados para la misma reserva -> el 1ro queda en retained y el 2do queda marcado
    from app.services import mercadopago_service

    simulate_net_error["active"] = False
    p1 = mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "950001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": payable,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
            "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
        },
    )
    p2 = mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "950002",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": payable,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
            "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
        },
    )
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.status == PaymentStatus.retained
    assert p2.status != PaymentStatus.retained
    assert p2.gateway_metadata.get("needs_refund") is True
    assert refund_calls == []  # Mercado Pago solo cobra: no hubo ningún reembolso por API


def test_wp04_wp06_chargeback_debt_cancelled_payout_no_show_and_fee_details(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """
    WP-04 & WP-06:
    1) WP-06: `GET /payments/platform-instructions` devuelve `gateway_fee_rate` y `gateway_fixed_fee_pen`,
       y un pago aprobado con `fee_details` guarda `gateway_fee_actual`, `gateway_fee_estimated` y `gateway_fee_diff`.
    2) WP-04 (D3): Contracargo posterior a liberación (`PaymentStatus.released` -> `charged_back`) registra
       deuda del músico (`total_chargeback_debt`) y se descuenta automáticamente en el siguiente desembolso.
    3) WP-04 (P-05): Reserva cancelada con retención a favor del músico aparece como `payable` en
       `musician/earnings` y puede incluirse en `request-payout`.
    4) WP-04 (A2): Reserva en `payment_retained` cuyo evento ya finalizó sin reseña del contratista aparece
       en `/admin/settlements` como `awaiting_reviews` y el admin puede liquidarla manualmente.
    5) WP-04 (Paso 5): `POST /bookings/{id}/report-no-show` abre queja `[NO-SHOW]` al iniciar el evento.
    """
    from datetime import datetime
    from app.services import mercadopago_service

    admin = User(
        email="admin.wp04@test.com",
        fullname="Admin WP04",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin)
    musician_user, contractor_user, b1 = _create_signed_booking(db_session, price=Decimal("400.00"))
    payable_b1 = float(contractor_payable_total(b1))

    # 1. WP-06: Consultar configuración de tarifas y registrar pago con fee_details
    _auth_cookie(client, contractor_user)
    res_inst = client.get("/api/v1/payments/platform-instructions")
    assert res_inst.status_code == 200
    inst_data = res_inst.json()
    assert inst_data["gateway_fee_rate"] == 0.0412
    assert inst_data["gateway_fixed_fee_pen"] == 1.18

    p_b1 = mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "960001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": payable_b1,
            "currency_id": "PEN",
            "external_reference": str(b1.id),
            "metadata": {"booking_id": str(b1.id), "payment_type": "full"},
            "fee_details": [{"type": "mercadopago_fee", "amount": 19.50}],
        },
    )
    db_session.refresh(p_b1)
    assert p_b1.gateway_metadata.get("gateway_fee_actual") == 19.50
    assert p_b1.gateway_metadata.get("gateway_fee_estimated") > 0
    assert "gateway_fee_diff" in p_b1.gateway_metadata

    # 2. WP-04 (D3): Simular que p_b1 ya fue desembolsado (released) y luego sufre un contracargo (charged_back)
    p_b1.status = PaymentStatus.released
    p_b1.released_at = datetime.utcnow()
    b1.status = BookingStatus.completed
    db_session.commit()

    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "960001",
            "status": "charged_back",
            "status_detail": "charged_back",
            "transaction_amount": payable_b1,
            "currency_id": "PEN",
            "external_reference": str(b1.id),
            "metadata": {"booking_id": str(b1.id), "payment_type": "full"},
        },
    )
    db_session.refresh(p_b1)
    assert p_b1.status == PaymentStatus.refunded
    assert p_b1.gateway_metadata.get("chargeback_after_release") is True
    assert p_b1.gateway_metadata.get("musician_chargeback_remaining") == 400.0

    # 3. WP-04 (P-05): Crear una segunda reserva (b2) del mismo músico por S/ 600, cancelada < 7 días (0% reembolso)
    b2 = Booking(
        musician_id=b1.musician_id,
        contractor_id=b1.contractor_id,
        event_type="Boda",
        event_date=date.today() + timedelta(days=3),
        start_time=dt_time(20, 0),
        end_time=dt_time(21, 0),
        location_city="Lima",
        location_address="Av. Larco 500",
        price_agreed=Decimal("600.00"),
        platform_fee_percent=Decimal("2.00"),
        status=BookingStatus.cancelled,
        cancellation_refund_status="not_applicable",
    )
    db_session.add(b2)
    db_session.flush()
    payable_b2 = float(contractor_payable_total(b2))
    p_b2 = Payment(
        booking_id=b2.id,
        amount=Decimal(str(payable_b2)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        gateway_provider="mercadopago",
        gateway_payment_id="960002",
    )
    db_session.add(p_b2)
    db_session.commit()

    # Verificar que en musician/earnings aparece b2 como payable (S/ 600), la deuda por contracargo (S/ 400),
    # y el neto por retirar (total_app_debt) es S/ 200.00
    _auth_cookie(client, musician_user)
    res_earn = client.get("/api/v1/payments/musician/earnings")
    assert res_earn.status_code == 200
    earn_json = res_earn.json()
    assert earn_json["total_chargeback_debt"] == 400.0
    assert earn_json["total_app_debt"] == 200.0

    # Solicitar retiro incluye la reserva cancelada b2 y descuenta el contracargo (monto solicitado = 200.0)
    res_req = client.post("/api/v1/payments/musician/request-payout")
    assert res_req.status_code == 200, res_req.text
    req_data = res_req.json()
    assert req_data["amount"] == 200.0

    # Cuando el admin procesa la solicitud de retiro, se descuenta la deuda por contracargo (musician_chargeback_remaining -> 0)
    _auth_cookie(client, admin)
    res_proc = client.post(
        f"/api/v1/admin/payout-requests/{req_data['id']}/process",
        json={"notes": "Desembolso con descuento de contracargo"},
    )
    assert res_proc.status_code == 200, res_proc.text
    db_session.expire_all()
    db_session.refresh(p_b1)
    db_session.refresh(p_b2)
    assert p_b1.gateway_metadata.get("musician_chargeback_remaining") == 0.0
    assert p_b2.status == PaymentStatus.released
    assert p_b2.gateway_metadata.get("chargeback_deducted") == 400.0

    # 4. WP-04 (A2): Reserva b3 cuyo evento ya finalizó (ayer) pero sigue en `payment_retained` sin reseñas
    b3 = Booking(
        musician_id=b1.musician_id,
        contractor_id=b1.contractor_id,
        event_type="Cumpleaños",
        event_date=date.today() - timedelta(days=2),
        start_time=dt_time(18, 0),
        end_time=dt_time(19, 0),
        location_city="Lima",
        location_address="Av. Javier Prado 900",
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("2.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(b3)
    db_session.flush()
    payable_b3 = float(contractor_payable_total(b3))
    p_b3 = Payment(
        booking_id=b3.id,
        amount=Decimal(str(payable_b3)),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        gateway_provider="mercadopago",
        gateway_payment_id="960003",
    )
    db_session.add(p_b3)
    db_session.commit()

    res_settlements = client.get("/api/v1/admin/settlements")
    assert res_settlements.status_code == 200
    b3_item = next((x for x in res_settlements.json() if x["booking_id"] == str(b3.id)), None)
    assert b3_item is not None
    assert b3_item["settlement_state"] == "awaiting_reviews"

    # El admin puede liquidar b3 manualmente aunque el contratista nunca dejó reseña
    res_settle_b3 = client.post(
        f"/api/v1/admin/settlements/{b3.id}/settle",
        json={"musician_amount": 500.0, "contractor_refund": 0.0, "notes": "Liquidación manual sin reseña"},
    )
    assert res_settle_b3.status_code == 200, res_settle_b3.text
    db_session.expire_all()
    db_session.refresh(b3)
    db_session.refresh(p_b3)
    assert b3.status == BookingStatus.completed
    assert p_b3.status == PaymentStatus.released

    # 5. WP-04 (Paso 5 & 6): No-show y expiración de `change_pending`
    b4 = Booking(
        musician_id=b1.musician_id,
        contractor_id=b1.contractor_id,
        event_type="Aniversario",
        event_date=date.today() - timedelta(days=1),
        start_time=dt_time(15, 0),
        end_time=dt_time(17, 0),
        location_city="Lima",
        location_address="Calle Los Pinos 123",
        price_agreed=Decimal("450.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(b4)
    db_session.commit()

    _auth_cookie(client, contractor_user)
    res_noshow = client.post(
        f"/api/v1/bookings/{b4.id}/report-no-show",
        json={"details": "Esperamos 45 minutos y nunca llegaron."},
    )
    assert res_noshow.status_code == 200, res_noshow.text
    assert res_noshow.json()["mode"] == "no_show_complaint"
    assert "[NO-SHOW]" in res_noshow.json()["complaint"]["reason"]
