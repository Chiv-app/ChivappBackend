"""Tests de regresión para la Fase 2 del Plan de Cierre (Dinero, Pagos, Retiros y Subidas)."""

from __future__ import annotations

import inspect
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.api.v1.endpoints.payments import mercadopago_webhook
from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.notification import Notification
from app.models.payment import Payment, PaymentStatus
from app.models.payout_request import PayoutRequest, PayoutRequestBooking
from app.models.profile_status import ProfileStatus
from app.models.uploaded_file import UploadedFile
from app.models.user import User, UserRole
from app.services import mercadopago_service
from app.services.contract_pdf import compute_contract_body_sha256
from app.services.settlement import release_retained_payments, settlement_state_for_booking


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def _seed_parties_and_booking(
    db: Session,
    *,
    status: BookingStatus = BookingStatus.accepted,
    event_date: date | None = None,
    start_time_val: time = time(18, 0),
    end_time_val: time = time(20, 0),
    price: Decimal = Decimal("500.00"),
) -> tuple[User, MusicianProfile, User, ContractorProfile, Booking]:
    suffix = uuid.uuid4().hex[:8]
    musician_user = User(
        email=f"mus_f2_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.musician,
        fullname="Músico Fase2",
        is_verified=True,
        is_active=True,
    )
    contractor_user = User(
        email=f"con_f2_{suffix}@test.com",
        password_hash=hash_password("Pass1234!"),
        role=UserRole.contractor,
        fullname="Contratista Fase2",
        is_verified=True,
        is_active=True,
    )
    db.add_all([musician_user, contractor_user])
    db.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name=f"Mariachi F2 {suffix}",
        slug=f"mariachi-f2-{suffix}",
        bio="Bio de prueba",
        genres=["Mariachi"],
        instruments=["Trompeta"],
        location_city="Lima",
        availability_type=AvailabilityType.both,
        price_per_hour=Decimal("250.00"),
        signature_image_url="/uploads/private/mus_sig.png",
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
        event_date=event_date or (date.today() + timedelta(days=10)),
        start_time=start_time_val,
        end_time=end_time_val,
        location_address="Av. Arequipa 1234",
        location_city="Lima",
        price_agreed=price,
        platform_fee_amount=Decimal("50.00"),
        platform_fee_percent=Decimal("10.00"),
        status=status,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return musician_user, musician, contractor_user, contractor, booking


# ---------------------------------------------------------------------------
# BUG4-11: mercadopago_webhook es síncrono (def, no async def)
# ---------------------------------------------------------------------------
def test_bug4_11_mercadopago_webhook_is_sync_function():
    assert not inspect.iscoroutinefunction(mercadopago_webhook), (
        "mercadopago_webhook debe ser def síncrono para ejecutarse en el threadpool de FastAPI"
    )


# ---------------------------------------------------------------------------
# SEC4-09 / SEC3-06: contract_body_sha256 obligatorio al aceptar/firmar
# ---------------------------------------------------------------------------
def test_sec4_09_contract_sha256_required_and_verified(client: TestClient, db_session: Session):
    _, _, contractor_user, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.accepted,
    )
    headers = _auth_headers(contractor_user)

    # 1. Crear archivo privado de firma perteneciente al contratista
    sig_filename = f"{uuid.uuid4().hex}.png"
    db_session.add(
        UploadedFile(
            filename=sig_filename,
            owner_user_id=contractor_user.id,
            content_type="image/png",
            size_bytes=128,
        )
    )
    db_session.commit()
    sig_url = f"/uploads/private/{sig_filename}"

    # 2. accept-quote sin contract_body_sha256 -> 422
    r_missing = client.post(
        f"/api/v1/bookings/{booking.id}/accept-quote",
        headers=headers,
        json={"signature_image_url": sig_url},
    )
    assert r_missing.status_code == 422

    # 3. accept-quote con hash distinto -> 409
    r_mismatch = client.post(
        f"/api/v1/bookings/{booking.id}/accept-quote",
        headers=headers,
        json={
            "signature_image_url": sig_url,
            "contract_body_sha256": "0" * 64,
        },
    )
    assert r_mismatch.status_code == 409

    # 4. Preference request con signature_image_url pero sin contract_body_sha256 -> 422
    r_pref_missing = client.post(
        "/api/v1/payments/mercadopago/preference",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "signature_image_url": sig_url,
        },
    )
    assert r_pref_missing.status_code == 422

    # 5. Process payment con signature_image_url pero sin contract_body_sha256 -> 422
    r_proc_missing = client.post(
        "/api/v1/payments/mercadopago/process",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "token": "tok_test_123",
            "payment_method_id": "yape",
            "signature_image_url": sig_url,
        },
    )
    assert r_proc_missing.status_code == 422

    # 6. Obtener contrato vía GET (que devuelve body_sha256) y aceptar con el hash real -> 200
    r_contract = client.get(
        f"/api/v1/contracts/booking/{booking.id}",
        headers=headers,
    )
    assert r_contract.status_code == 200
    real_sha = r_contract.json()["body_sha256"]
    assert len(real_sha) == 64
    r_ok = client.post(
        f"/api/v1/bookings/{booking.id}/accept-quote",
        headers=headers,
        json={
            "signature_image_url": sig_url,
            "contract_body_sha256": real_sha,
        },
    )
    assert r_ok.status_code == 200


# ---------------------------------------------------------------------------
# SEC6-04: Firmar contrato en preference/process usa flush() y no suelta FOR UPDATE
# ---------------------------------------------------------------------------
def test_sec6_04_signing_in_preference_does_not_commit_before_preference(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    _, _, contractor_user, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.accepted,
    )
    headers = _auth_headers(contractor_user)

    sig_filename = f"{uuid.uuid4().hex}.png"
    db_session.add(
        UploadedFile(
            filename=sig_filename,
            owner_user_id=contractor_user.id,
            content_type="image/png",
            size_bytes=128,
        )
    )
    contract = Contract(
        booking_id=booking.id,
        title="Contrato de Servicios Musicales",
        body="<p>Cuerpo del contrato con longitud suficiente para firmar.</p>",
    )
    db_session.add(contract)
    db_session.commit()
    real_sha = compute_contract_body_sha256(contract.title, contract.body)

    # Simulamos que create_preference falla con ValueError: si hubiera habido db.commit()
    # al firmar, la reserva habría quedado en contract_signed pese al fallo.
    def _boom_create_preference(**kwargs):
        raise ValueError("Fallo simulado de pasarela")

    monkeypatch.setattr(mercadopago_service, "create_preference", _boom_create_preference)

    resp = client.post(
        "/api/v1/payments/mercadopago/preference",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "signature_image_url": f"/uploads/private/{sig_filename}",
            "contract_body_sha256": real_sha,
        },
    )
    assert resp.status_code == 400
    db_session.expire_all()
    refreshed_booking = db_session.get(Booking, booking.id)
    assert refreshed_booking.status == BookingStatus.accepted


# ---------------------------------------------------------------------------
# SEC5-10: Pagos en estado terminal (refunded / rejected) no reviven a retained
# ---------------------------------------------------------------------------
def test_sec5_10_terminal_payment_does_not_resurrect_on_late_approved_webhook(
    db_session: Session,
):
    _, _, _, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.cancelled,
    )
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("500.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.refunded,
        gateway_provider="mercadopago",
        gateway_payment_id="99887766",
        gateway_metadata={"status": "refunded"},
    )
    db_session.add(payment)
    db_session.commit()

    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "99887766",
            "status": "approved",
            "transaction_amount": 500.0,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
            "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
        },
    )
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.status == PaymentStatus.refunded
    assert booking.status == BookingStatus.cancelled


# ---------------------------------------------------------------------------
# SEC4-05 / BUG5-13 / BUG5-07:
# - in_mediation bloquea liquidación (settlement_state == "disputed" y release falla)
# - reembolso parcial mantiene retained y guarda partial_refund_amount
# - contracargo externo cancela reserva, borra evento de calendario y notifica a ambos
# ---------------------------------------------------------------------------
def test_sec4_05_bug5_13_bug5_07_mediation_partial_refund_and_chargeback(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    musician_user, _, contractor_user, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.payment_retained,
    )
    booking.calendar_event_id = "cal_evt_123"
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("500.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        retained_at=datetime.utcnow(),
        gateway_provider="mercadopago",
        gateway_payment_id="11223344",
        gateway_metadata={"status": "approved"},
    )
    db_session.add(payment)
    db_session.commit()

    # 1) in_mediation bloquea liquidación
    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "11223344",
            "status": "in_mediation",
            "transaction_amount": 500.0,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.gateway_metadata.get("in_mediation") is True
    assert settlement_state_for_booking(booking, None, 500.0, 0.0) == "disputed"
    with pytest.raises(ValueError, match="mediación"):
        release_retained_payments(db_session, booking.id)

    # 2) Reembolso parcial mantiene retained y registra partial_refund_amount
    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "11223344",
            "status": "refunded",
            "transaction_amount": 500.0,
            "transaction_amount_refunded": 150.0,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.status == PaymentStatus.retained
    assert payment.gateway_metadata.get("partial_refund_amount") == 150.0
    assert booking.status == BookingStatus.payment_retained

    # 3) Contracargo total externo cancela reserva, invoca cancel_booking_calendar_event y notifica
    calendar_cancelled_ids: list[str] = []
    monkeypatch.setattr(
        mercadopago_service,
        "cancel_booking_calendar_event",
        lambda db, b: calendar_cancelled_ids.append(str(b.id)),
    )

    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "11223344",
            "status": "charged_back",
            "transaction_amount": 500.0,
            "transaction_amount_refunded": 500.0,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    db_session.refresh(payment)
    db_session.refresh(booking)
    assert payment.status == PaymentStatus.refunded
    assert booking.status == BookingStatus.cancelled
    assert str(booking.id) in calendar_cancelled_ids

    notifs = (
        db_session.query(Notification)
        .filter(
            Notification.type.in_(["booking_cancelled", "payment_chargeback"]),
            Notification.user_id.in_([musician_user.id, contractor_user.id]),
        )
        .all()
    )
    notified_user_ids = {n.user_id for n in notifs}
    assert musician_user.id in notified_user_ids
    assert contractor_user.id in notified_user_ids


# ---------------------------------------------------------------------------
# SEC4-12: _confirm_booking_notifications_and_calendar se ejecuta DESPUÉS del commit
# ---------------------------------------------------------------------------
def test_sec4_12_confirm_notifications_run_after_commit(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    _, _, _, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.contract_signed,
    )
    events: list[str] = []
    orig_commit = db_session.commit

    def _tracked_commit():
        events.append("commit")
        return orig_commit()

    monkeypatch.setattr(db_session, "commit", _tracked_commit)
    monkeypatch.setattr(
        mercadopago_service,
        "_confirm_booking_notifications_and_calendar",
        lambda db, b: events.append("confirm_side_effects"),
    )

    expected_total = float(mercadopago_service.expected_payment_amount(db_session, booking, "full"))
    mercadopago_service.process_approved_mercadopago_payment(
        db_session,
        {
            "id": "55667788",
            "status": "approved",
            "transaction_amount": expected_total,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
            "metadata": {"booking_id": str(booking.id), "payment_type": "full"},
        },
    )
    assert "commit" in events
    assert "confirm_side_effects" in events
    assert events.index("commit") < events.index("confirm_side_effects")


# ---------------------------------------------------------------------------
# BUG5-06: refund_flagged_payment_admin exige pasarela o manual_override + razón
# ---------------------------------------------------------------------------
def test_bug5_06_admin_refund_requires_gateway_or_manual_override(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core.config import settings

    _, _, _, _, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.payment_retained,
    )
    admin = User(
        email=f"admin_f2_{uuid.uuid4().hex[:6]}@test.com",
        password_hash=hash_password("AdminPass123!"),
        role=UserRole.admin,
        fullname="Admin F2",
        is_verified=True,
        is_active=True,
    )
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("500.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.initiated,
        gateway_provider="mercadopago",
        gateway_payment_id=None,  # Sin ID de pasarela
        gateway_metadata={"needs_refund": True, "needs_refund_reason": "duplicate"},
    )
    db_session.add_all([admin, payment])
    db_session.commit()

    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "")
    headers = _auth_headers(admin)

    # Sin manual_override -> 409
    r_conflict = client.post(
        f"/api/v1/admin/payments/{payment.id}/refund",
        headers=headers,
    )
    assert r_conflict.status_code == 409
    assert "panel de Mercado Pago" in r_conflict.json()["detail"]

    # Con manual_override=True y razón explícita -> 200
    r_ok = client.post(
        f"/api/v1/admin/payments/{payment.id}/refund",
        headers=headers,
        json={
            "manual_override": True,
            "manual_reason": "Reembolsado manualmente en el panel MP #OP-999",
        },
    )
    assert r_ok.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.refunded
    assert payment.gateway_metadata.get("manual_override") is True


# ---------------------------------------------------------------------------
# BUG5-08 / SEC5-12: force-complete exige >=48h desde fin de evento y sin quejas abiertas
# ---------------------------------------------------------------------------
def test_bug5_08_force_complete_requires_48h_and_no_open_complaint(
    client: TestClient,
    db_session: Session,
):
    admin = User(
        email=f"admin_fc_{uuid.uuid4().hex[:6]}@test.com",
        password_hash=hash_password("AdminPass123!"),
        role=UserRole.admin,
        fullname="Admin FC",
        is_verified=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    headers = _auth_headers(admin)

    # 1. Evento terminado ayer (< 48h) -> 400
    _, _, _, _, recent_booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.payment_retained,
        event_date=date.today() - timedelta(days=1),
        start_time_val=time(10, 0),
        end_time_val=time(12, 0),
    )
    r_too_soon = client.post(
        f"/api/v1/admin/bookings/{recent_booking.id}/force-complete",
        headers=headers,
    )
    assert r_too_soon.status_code == 400
    assert "48 horas" in r_too_soon.json()["detail"]

    # 2. Evento terminado hace 4 días (> 48h), pero con queja abierta -> 400
    _, _, contractor_user, _, old_booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.payment_retained,
        event_date=date.today() - timedelta(days=4),
        start_time_val=time(10, 0),
        end_time_val=time(12, 0),
    )
    complaint = BookingComplaint(
        booking_id=old_booking.id,
        opened_by_user_id=contractor_user.id,
        reason="Incumplimiento de horario: el grupo llegó tarde al evento.",
        status=BookingComplaintStatus.open,
    )
    db_session.add(complaint)
    db_session.commit()

    r_with_complaint = client.post(
        f"/api/v1/admin/bookings/{old_booking.id}/force-complete",
        headers=headers,
    )
    assert r_with_complaint.status_code == 400
    assert "queja abierta" in r_with_complaint.json()["detail"]

    # 3. Al resolver la queja -> 200
    complaint.status = BookingComplaintStatus.settled
    db_session.commit()
    r_ok = client.post(
        f"/api/v1/admin/bookings/{old_booking.id}/force-complete",
        headers=headers,
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == BookingStatus.completed.value


# ---------------------------------------------------------------------------
# BUG5-11 / SEC4-03: PayoutRequestBooking explícito y bloqueo de heurística ciega
# ---------------------------------------------------------------------------
def test_bug5_11_payout_request_booking_link_and_legacy_conflict(
    client: TestClient,
    db_session: Session,
):
    musician_user, musician, contractor_user, contractor, booking = _seed_parties_and_booking(
        db_session,
        status=BookingStatus.completed,
        event_date=date.today() - timedelta(days=5),
    )
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal("500.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.retained,
        retained_at=datetime.utcnow(),
    )
    review = BookingReview(
        booking_id=booking.id,
        author_user_id=contractor_user.id,
        rating=5,
        comment="Excelente show",
        is_final=True,
    )
    rec = ContractorRecommendation(
        booking_id=booking.id,
        musician_id=musician.id,
        contractor_id=contractor.id,
        rating=5,
        comment="Excelente cliente",
    )
    admin = User(
        email=f"admin_po_{uuid.uuid4().hex[:6]}@test.com",
        password_hash=hash_password("AdminPass123!"),
        role=UserRole.admin,
        fullname="Admin Payout",
        is_verified=True,
        is_active=True,
    )
    db_session.add_all([payment, review, rec, admin])
    db_session.commit()

    # 1. Músico solicita retiro -> se crea fila en payout_request_booking
    r_req = client.post(
        "/api/v1/payments/musician/request-payout",
        headers=_auth_headers(musician_user),
    )
    assert r_req.status_code == 200
    payout_id = uuid.UUID(r_req.json()["id"])

    links = (
        db_session.query(PayoutRequestBooking)
        .filter(PayoutRequestBooking.payout_request_id == payout_id)
        .all()
    )
    assert len(links) == 1
    assert links[0].booking_id == booking.id

    # 2. Si una solicitud heredada NO tiene filas en payout_request_booking ni UUIDs en notes,
    #    admin/payout-requests/{id}/process devuelve 409 en vez de adivinar por suma
    db_session.query(PayoutRequestBooking).filter(
        PayoutRequestBooking.payout_request_id == payout_id
    ).delete()
    payout_row = db_session.get(PayoutRequest, payout_id)
    payout_row.notes = "Solicitud heredada sin UUIDs"
    db_session.commit()

    r_legacy_fail = client.post(
        f"/api/v1/admin/payout-requests/{payout_id}/process",
        headers=_auth_headers(admin),
        json={"notes": "Intentando procesar sin vínculo"},
    )
    assert r_legacy_fail.status_code == 409
    assert "booking_ids" in r_legacy_fail.json()["detail"]

    # 3. Pasando explícitamente booking_ids en el body -> 200 y libera el pago
    r_explicit_ok = client.post(
        f"/api/v1/admin/payout-requests/{payout_id}/process",
        headers=_auth_headers(admin),
        json={"notes": "Procesado indicando booking_ids", "booking_ids": [str(booking.id)]},
    )
    assert r_explicit_ok.status_code == 200
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.released


# ---------------------------------------------------------------------------
# SEC5-11: id_document_url y signature_image_url exigen /uploads/private/ y propiedad
# ---------------------------------------------------------------------------
def test_sec5_11_profile_private_urls_reject_public_or_foreign_files(
    client: TestClient,
    db_session: Session,
):
    musician_user, _, contractor_user, _, _ = _seed_parties_and_booking(db_session)

    # 1. URL pública en id_document_url o signature_image_url -> 422
    r_pub_mus = client.put(
        "/api/v1/profiles/musician",
        headers=_auth_headers(musician_user),
        json={"id_document_url": "https://evil.example.com/dni.png"},
    )
    assert r_pub_mus.status_code == 422

    r_pub_sig = client.put(
        "/api/v1/profiles/musician",
        headers=_auth_headers(musician_user),
        json={"signature_image_url": "/uploads/public_sig.png"},
    )
    assert r_pub_sig.status_code == 422

    r_pub_con = client.put(
        "/api/v1/profiles/contractor",
        headers=_auth_headers(contractor_user),
        json={"id_document_url": "https://evil.example.com/dni.png"},
    )
    assert r_pub_con.status_code == 422

    # 2. Archivo privado subido por OTRO usuario -> 400
    other_file = f"{uuid.uuid4().hex}.png"
    db_session.add(
        UploadedFile(
            filename=other_file,
            owner_user_id=contractor_user.id,
            content_type="image/png",
            size_bytes=100,
        )
    )
    db_session.commit()

    r_foreign = client.put(
        "/api/v1/profiles/musician",
        headers=_auth_headers(musician_user),
        json={"id_document_url": f"/uploads/private/{other_file}"},
    )
    assert r_foreign.status_code == 400


# ---------------------------------------------------------------------------
# SEC-09, SEC-16: Límite de 20 subidas por hora por usuario en POST /api/v1/uploads/
# ---------------------------------------------------------------------------
def test_sec09_sec16_upload_rate_limit_per_user(
    client: TestClient,
    db_session: Session,
):
    _, _, contractor_user, _, _ = _seed_parties_and_booking(db_session)
    headers = _auth_headers(contractor_user)

    # Insertamos 20 archivos subidos en la última hora para este usuario
    for _ in range(20):
        db_session.add(
            UploadedFile(
                filename=f"{uuid.uuid4().hex}.png",
                owner_user_id=contractor_user.id,
                content_type="image/png",
                size_bytes=1024,
            )
        )
    db_session.commit()

    # La subida 21 dentro de la misma hora debe responder 429
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    resp = client.post(
        "/api/v1/uploads/",
        headers=headers,
        files={"file": ("test.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 429
