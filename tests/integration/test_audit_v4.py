"""Tests de integración para la ronda 4 de auditoría (v4)."""

import ast
import importlib
import pkgutil
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

import app
from app.core.config import Settings, settings
from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.core.timezone import now_peru
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile, ProfileStatus
from app.models.payment import Payment, PaymentStatus
from app.models.payout_request import PayoutRequest
from app.models.user import User, UserRole
from app.services.cancellation_policy import contractor_refund_percent
from app.services.email.auth_emails import issue_email_verification_token
from app.services.mercadopago_service import process_approved_mercadopago_payment
from app.services.platform_payment import contractor_payable_total

pytestmark = pytest.mark.db


def _setup_users(db):
    admin = User(
        email="admin_v4@test.com",
        fullname="Admin V4",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    musician_user = User(
        email="musician_v4@test.com",
        fullname="Mariachi V4",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    contractor_user = User(
        email="contractor_v4@test.com",
        fullname="Contratista V4",
        phone="999888777",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add_all([admin, musician_user, contractor_user])
    db.flush()

    musician = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi V4",
        slug=f"mariachi-v4-{musician_user.id.hex[:6]}",
        status=ProfileStatus.published,
        contract_template_title="Contrato V4",
        contract_template_body=(
            "<p>Contrato de prestación de servicios musicales profesionales celebrado entre las partes "
            "por el monto total de {{monto_total}} para la fecha {{fecha_evento}} en {{lugar_evento}}.</p>"
        ),
    )
    contractor = ContractorProfile(
        user_id=contractor_user.id,
        status=ProfileStatus.published,
    )
    db.add_all([musician, contractor])
    db.commit()
    return admin, musician_user, musician, contractor_user, contractor


def _auth_cookie(client, user: User):
    client.cookies.clear()
    client.cookies.set(
        "access_token",
        create_access_token(
            subject=str(user.id),
            role=user.role.value,
            version=user.token_version or 0,
        ),
    )


def test_sec4_01_verify_email_preserves_password_hash_and_rotates_token_version(
    client, db_session
):
    """SEC4-01 / BUG4-01: verificar correo NO borra password_hash y rota token_version."""
    user = User(
        email="verify_pwd_v4@test.com",
        fullname="Usuario Con Clave",
        role=UserRole.contractor,
        password_hash=hash_password("Secr3tPass!99"),
        is_active=True,
        is_verified=False,
        token_version=1,
    )
    db_session.add(user)
    db_session.flush()
    raw_token = issue_email_verification_token(user)
    db_session.commit()

    # Antes de verificar, login por contraseña debe estar bloqueado (403)
    res_pre = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "Secr3tPass!99"},
    )
    assert res_pre.status_code == 403

    # Verificar correo desde el mismo navegador del usuario (sesión activa): conserva password_hash y rota token_version
    _auth_cookie(client, user)
    res_ver = client.post(f"/api/v1/auth/verify-email/{raw_token}")
    assert res_ver.status_code == 200

    db_session.refresh(user)
    assert user.password_hash is not None
    assert user.email_verified_at is not None
    assert (user.token_version or 0) == 2

    # Después de verificar, login con la contraseña original funciona
    client.cookies.clear()
    res_login = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "Secr3tPass!99"},
    )
    assert res_login.status_code == 200


def test_bug4_09_mercadopago_rejects_underpayment_and_overpayment(db_session):
    """BUG4-09: subpago y sobrepago mayores a S/ 0.01 no aprueban la reserva y marcan needs_refund."""
    _, _, musician, _, contractor = _setup_users(db_session)
    tomorrow = (now_peru() + timedelta(days=3)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=tomorrow,
        start_time=time(19, 0),
        end_time=time(21, 0),
        event_type="Aniversario",
        location_city="Lima",
        location_address="Av. Larco 100",
        status=BookingStatus.accepted,
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add(booking)
    db_session.commit()

    # Subpago (S/ 450 vs S/ 500)
    res_under = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-UNDER-409",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 450.00,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    assert res_under is not None
    assert res_under.status == PaymentStatus.initiated
    db_session.refresh(booking)
    assert booking.status == BookingStatus.accepted
    assert res_under.gateway_metadata.get("needs_refund") is True
    assert res_under.gateway_metadata.get("needs_refund_reason") == "invalid_amount_or_currency"

    # Sobrepago (S/ 650 vs S/ 500)
    res_over = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-OVER-409",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 650.00,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    assert res_over is not None
    assert res_over.status == PaymentStatus.initiated
    db_session.refresh(booking)
    assert booking.status == BookingStatus.accepted
    assert res_over.gateway_metadata.get("needs_refund") is True
    assert res_over.gateway_metadata.get("needs_refund_reason") == "invalid_amount_or_currency"


def test_sec4_05_bug4_03_mercadopago_post_approval_chargeback_and_refund(db_session):
    """SEC4-05 / BUG4-03: contracargo o reembolso posterior en MP revierte Payment.retained."""
    _, _, musician, _, contractor = _setup_users(db_session)
    future_date = (now_peru() + timedelta(days=5)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(20, 0),
        end_time=time(22, 0),
        event_type="Boda",
        location_city="Lima",
        location_address="Av. Arequipa 500",
        status=BookingStatus.accepted,
        price_agreed=Decimal("800.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add(booking)
    db_session.commit()

    expected_total = float(contractor_payable_total(booking))

    # 1. Pago aprobado
    payment = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-CB-405",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_total,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    assert payment is not None
    assert payment.status == PaymentStatus.retained
    db_session.refresh(booking)
    assert booking.status == BookingStatus.payment_retained

    # 2. Webhook posterior con charged_back
    updated = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-CB-405",
            "status": "charged_back",
            "status_detail": "settled",
            "transaction_amount": expected_total,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    assert updated is not None
    assert updated.status == PaymentStatus.refunded
    assert updated.gateway_metadata.get("post_approval_event") == "charged_back"
    db_session.refresh(booking)
    assert booking.status == BookingStatus.cancelled


def test_bug4_05_event_passed_blocks_payment_and_flags_late_webhook(
    client, db_session
):
    """BUG4-05: no permite iniciar pago ni retener webhook de evento cuya fecha ya pasó."""
    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    yesterday = (now_peru() - timedelta(days=2)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=yesterday,
        start_time=time(18, 0),
        end_time=time(20, 0),
        event_type="Cumpleaños",
        location_city="Lima",
        location_address="Jr. Unión 123",
        status=BookingStatus.accepted,
        price_agreed=Decimal("400.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add(booking)
    db_session.commit()

    _auth_cookie(client, contractor_user)
    res_pref = client.post(
        "/api/v1/payments/mercadopago/preference",
        json={"booking_id": str(booking.id)},
    )
    assert res_pref.status_code == 400
    assert "pasó" in res_pref.json()["detail"].lower() or "inició" in res_pref.json()["detail"].lower()

    # Si llega un webhook tardío de MP sobre reserva con fecha pasada:
    expected_late = float(contractor_payable_total(booking))
    late_res = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-LATE-405",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_late,
            "currency_id": "PEN",
            "external_reference": str(booking.id),
        },
    )
    assert late_res is not None
    assert late_res.status == PaymentStatus.initiated
    assert late_res.gateway_metadata.get("needs_refund") is True
    assert late_res.gateway_metadata.get("needs_refund_reason") == "event_passed"


def test_sec4_04_bug4_04_booking_closes_only_when_both_parties_review(
    client, db_session
):
    """Decisión 3 (SEC4-04 / BUG4-04): la reserva solo pasa a completed cuando contratista Y músico reseñan."""
    _, musician_user, musician, contractor_user, contractor = _setup_users(db_session)
    past_date = (now_peru() - timedelta(days=1)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=past_date,
        start_time=time(10, 0),
        end_time=time(12, 0),
        event_type="Serenata",
        location_city="Lima",
        location_address="Av. Brasil 200",
        status=BookingStatus.in_progress,
        price_agreed=Decimal("500.00"),
    )
    db_session.add(booking)
    db_session.commit()

    # 1. Reacción con emoji durante el evento NO cuenta como reseña final
    emoji_rev = BookingReview(
        booking_id=booking.id,
        rating=5,
        emoji="🔥",
        comment=None,
    )
    db_session.add(emoji_rev)
    db_session.commit()

    # 2. Músico intenta cerrar o recomienda al contratista sin reseña final del contratista
    _auth_cookie(client, musician_user)
    res_complete_early = client.post(f"/api/v1/bookings/{booking.id}/complete")
    assert res_complete_early.status_code == 400

    res_rec = client.post(
        f"/api/v1/bookings/{booking.id}/recommend-contractor",
        json={"rating": 5, "comment": "Excelente anfitrión y trato puntual."},
    )
    assert res_rec.status_code == 200
    db_session.refresh(booking)
    # Aún falta la reseña final del contratista -> sigue in_progress
    assert booking.status == BookingStatus.in_progress

    # 3. Contratista publica su reseña final -> ahora están ambas y se cierra la reserva
    _auth_cookie(client, contractor_user)
    res_final = client.post(
        f"/api/v1/bookings/{booking.id}/final-review",
        json={"rating": 5, "comment": "Impecable presentación musical de principio a fin."},
    )
    assert res_final.status_code == 200
    db_session.refresh(booking)
    assert booking.status == BookingStatus.completed


def test_bug4_06_cancellation_policy_at_15_days_is_100_percent(client):
    """Decisión 2 (FE4-04 / BUG4-06): a exactamente 15 días calendario el reembolso es 100 %."""
    pct_15, rule_15 = contractor_refund_percent(15)
    assert pct_15 == 100
    assert "100 %" in rule_15

    pct_14, rule_14 = contractor_refund_percent(14)
    assert pct_14 == 50
    assert "50 %" in rule_14

    pct_6, _ = contractor_refund_percent(6)
    assert pct_6 == 0

    # Endpoint público de política de cancelación
    res_pol = client.get("/api/v1/bookings/cancellation-policy")
    assert res_pol.status_code == 200
    body = res_pol.json()
    assert body["full_refund_min_days"] == 15
    assert body["partial_refund_min_days"] == 7


def test_sec4_03_bug4_02_payout_process_releases_only_linked_bookings(
    client, db_session
):
    """SEC4-03 / BUG4-02: procesar un retiro solo libera las reservas cubiertas por la solicitud."""
    admin, musician_user, musician, _, contractor = _setup_users(db_session)
    past_date = (now_peru() - timedelta(days=5)).date()

    b1 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=past_date,
        start_time=time(14, 0),
        end_time=time(16, 0),
        event_type="Evento 1",
        location_city="Lima",
        location_address="Calle 1",
        status=BookingStatus.completed,
        price_agreed=Decimal("300.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    b2 = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=past_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        event_type="Evento 2",
        location_city="Lima",
        location_address="Calle 2",
        status=BookingStatus.completed,
        price_agreed=Decimal("400.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add_all([b1, b2])
    db_session.flush()

    p1 = Payment(
        booking_id=b1.id,
        amount=contractor_payable_total(b1),
        currency="PEN",
        payment_type="deposit",
        status=PaymentStatus.retained,
        retained_at=datetime.utcnow() - timedelta(days=4),
    )
    p2 = Payment(
        booking_id=b2.id,
        amount=contractor_payable_total(b2),
        currency="PEN",
        payment_type="deposit",
        status=PaymentStatus.retained,
        retained_at=datetime.utcnow() - timedelta(days=4),
    )
    # Solicitud de retiro vinculada únicamente a b1 (S/ 300)
    req = PayoutRequest(
        musician_id=musician.id,
        amount=Decimal("300.00"),
        notes=f"[Sistema] Reservas cubiertas: {b1.id}",
        status="pending",
    )
    db_session.add_all([p1, p2, req])
    db_session.commit()

    _auth_cookie(client, admin)
    res = client.post(
        f"/api/v1/admin/payout-requests/{req.id}/process",
        json={"notes": "Pago de Evento 1"},
    )
    assert res.status_code == 200
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.status == PaymentStatus.released
    # p2 NO debe liberarse
    assert p2.status == PaymentStatus.retained


def test_sec4_06_admin_refund_flagged_payment_and_force_complete(client, db_session):
    """SEC4-06 y BUG4-08: reembolso admin para needs_refund y cierre admin force-complete."""
    admin, _, musician, _, contractor = _setup_users(db_session)
    past_date = (now_peru() - timedelta(days=3)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=past_date,
        start_time=time(15, 0),
        end_time=time(17, 0),
        event_type="Graduación",
        location_city="Lima",
        location_address="Av. Universitaria 1000",
        status=BookingStatus.payment_retained,
        price_agreed=Decimal("500.00"),
    )
    db_session.add(booking)
    db_session.flush()

    flagged_payment = Payment(
        booking_id=booking.id,
        amount=Decimal("500.00"),
        currency="PEN",
        payment_type="full",
        status=PaymentStatus.initiated,
        gateway_provider="mercadopago",
        gateway_payment_id="MP-FLAGGED-406",
        gateway_metadata={"needs_refund": True, "needs_refund_reason": "late_webhook"},
    )
    db_session.add(flagged_payment)
    db_session.commit()

    _auth_cookie(client, admin)
    with patch("app.services.mercadopago_service.issue_refund", return_value={"id": "REF-999"}):
        res_ref = client.post(f"/api/v1/admin/payments/{flagged_payment.id}/refund")
    assert res_ref.status_code == 200
    db_session.refresh(flagged_payment)
    assert flagged_payment.status == PaymentStatus.refunded
    assert flagged_payment.gateway_metadata.get("needs_refund") is False

    res_fc = client.post(f"/api/v1/admin/bookings/{booking.id}/force-complete")
    assert res_fc.status_code == 200
    db_session.refresh(booking)
    assert booking.status == BookingStatus.completed

    # Reembolso idempotente: intentar reembolsar el mismo pago por segunda vez devuelve 400
    res_ref_dup = client.post(f"/api/v1/admin/payments/{flagged_payment.id}/refund")
    assert res_ref_dup.status_code == 400


def test_task38_mercadopago_webhook_idempotency_cancelled_and_overlap(db_session):
    """Tarea 38: webhook repetido idempotente, pago sobre reserva cancelada y solape de reserva."""
    _, _, musician, _, contractor = _setup_users(db_session)
    future_date = (now_peru() + timedelta(days=7)).date()

    b_main = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        event_type="Cena",
        location_city="Lima",
        location_address="Av. Pardo 100",
        status=BookingStatus.accepted,
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    b_overlap = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(19, 0),
        end_time=time(21, 0),
        event_type="Cóctel",
        location_city="Lima",
        location_address="Av. Pardo 200",
        status=BookingStatus.accepted,
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    b_cancelled = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(12, 0),
        end_time=time(14, 0),
        event_type="Almuerzo",
        location_city="Lima",
        location_address="Av. Pardo 300",
        status=BookingStatus.cancelled,
        price_agreed=Decimal("500.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add_all([b_main, b_overlap, b_cancelled])
    db_session.commit()

    expected_amt = float(contractor_payable_total(b_main))

    # 1. Primer webhook sobre b_main -> aprueba y retiene
    p1 = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-IDEMP-38",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_amt,
            "currency_id": "PEN",
            "external_reference": str(b_main.id),
        },
    )
    assert p1 is not None
    assert p1.status == PaymentStatus.retained
    db_session.refresh(b_main)
    assert b_main.status == BookingStatus.payment_retained

    # 2. Webhook repetido con mismo gateway_payment_id -> idempotente, mismo registro
    p1_dup = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-IDEMP-38",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_amt,
            "currency_id": "PEN",
            "external_reference": str(b_main.id),
        },
    )
    assert p1_dup is not None
    assert p1_dup.id == p1.id
    assert (
        db_session.query(Payment)
        .filter(Payment.gateway_payment_id == "MP-IDEMP-38")
        .count()
        == 1
    )

    # 3. Pago sobre reserva que ahora solapa con b_main -> marca needs_refund=overlap_conflict
    p_overlap = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-OVERLAP-38",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_amt,
            "currency_id": "PEN",
            "external_reference": str(b_overlap.id),
        },
    )
    assert p_overlap is not None
    assert p_overlap.status == PaymentStatus.initiated
    assert p_overlap.gateway_metadata.get("needs_refund") is True
    assert p_overlap.gateway_metadata.get("needs_refund_reason") == "schedule_overlap"

    # 4. Pago tardío sobre reserva cancelada -> marca needs_refund=late_webhook
    p_canc = process_approved_mercadopago_payment(
        db_session,
        {
            "id": "MP-CANCELLED-38",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": expected_amt,
            "currency_id": "PEN",
            "external_reference": str(b_cancelled.id),
        },
    )
    assert p_canc is not None
    assert p_canc.status == PaymentStatus.initiated
    assert p_canc.gateway_metadata.get("needs_refund") is True
    assert p_canc.gateway_metadata.get("needs_refund_reason") == "duplicate_or_unpayable"
    db_session.refresh(b_cancelled)
    assert b_cancelled.status == BookingStatus.cancelled


def test_import_and_settings_integrity():
    """Tarea 39: todos los módulos bajo app/ importan sin errores y todo settings.<ATTR> existe en Settings."""
    app_root = Path(app.__file__).resolve().parent
    valid_settings_attrs = set(Settings.model_fields.keys()) | {
        name for name, val in vars(Settings).items() if not name.startswith("_")
    }

    missing_attrs: list[str] = []
    for py_file in app_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
            ):
                if node.attr not in valid_settings_attrs and not hasattr(settings, node.attr):
                    rel = py_file.relative_to(app_root.parent)
                    missing_attrs.append(f"{rel}:{node.lineno} -> settings.{node.attr}")

    assert not missing_attrs, f"Atributos inexistentes en settings: {missing_attrs}"

    # Verificar que todos los submódulos de app.api y app.services importen limpiamente
    for pkg_name in ("app.api", "app.services", "app.models", "app.schemas"):
        pkg = importlib.import_module(pkg_name)
        for mod_info in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
            importlib.import_module(mod_info.name)


def test_fe5_01_pending_mercadopago_persists_initiated_and_blocks_second_charge(
    client, db_session, monkeypatch
):
    """FE5-01: un cobro en estado pending/in_process persiste Payment(initiated) y bloquea un 2.º POST /process con 409."""
    monkeypatch.setattr(settings, "MERCADO_PAGO_ACCESS_TOKEN", "TEST-TOKEN-501")
    monkeypatch.setattr(settings, "MERCADO_PAGO_PUBLIC_KEY", "TEST-PUBKEY-501")

    _, _, musician, contractor_user, contractor = _setup_users(db_session)
    future_date = (now_peru() + timedelta(days=9)).date()

    booking = Booking(
        musician_id=musician.id,
        contractor_id=contractor.id,
        event_date=future_date,
        start_time=time(18, 0),
        end_time=time(20, 0),
        event_type="Aniversario",
        location_city="Lima",
        location_address="Av. Larco 500",
        status=BookingStatus.contract_signed,
        price_agreed=Decimal("400.00"),
        platform_fee_percent=Decimal("0.00"),
        platform_fee_amount=Decimal("0.00"),
    )
    db_session.add(booking)
    db_session.commit()

    class _FakeResp:
        status_code = 201
        text = "{}"

        def json(self):
            return {
                "id": "998877501",
                "status": "in_process",
                "status_detail": "pending_review_manual",
                "transaction_amount": 400.0,
                "currency_id": "PEN",
                "external_reference": str(booking.id),
            }

    class _FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            return _FakeResp()

    _auth_cookie(client, contractor_user)
    initial_check = client.get(f"/api/v1/payments/mercadopago/check-status/{booking.id}")
    assert initial_check.status_code == 200, initial_check.text
    assert initial_check.json()["status"] == "not_started"
    assert initial_check.json()["payment_id"] is None

    with patch("app.services.mercadopago_service.httpx.Client", _FakeHttpxClient):
        res1 = client.post(
            "/api/v1/payments/mercadopago/process",
            json={
                "booking_id": str(booking.id),
                "payment_type": "full",
                "token": "tok_test_123",
                "payment_method_id": "visa",
                "installments": 1,
                "payer_email": contractor_user.email,
            },
        )
    assert res1.status_code == 200, res1.text
    body1 = res1.json()
    assert body1["status"] == "in_process"
    assert body1["payment_id"] == "998877501"

    pending_check = client.get(f"/api/v1/payments/mercadopago/check-status/{booking.id}")
    assert pending_check.status_code == 200, pending_check.text
    assert pending_check.json()["status"] == "pending"
    assert pending_check.json()["payment_id"] == "998877501"

    persisted = (
        db_session.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.gateway_payment_id == "998877501",
        )
        .first()
    )
    assert persisted is not None
    assert persisted.status == PaymentStatus.initiated

    # Un segundo intento mientras sigue en verificación debe rechazarse con 409 sin tocar Mercado Pago
    with patch(
        "app.services.mercadopago_service.httpx.Client",
        side_effect=AssertionError("No debe invocar httpx.Client en un reintento bloqueado"),
    ):
        res2 = client.post(
            "/api/v1/payments/mercadopago/process",
            json={
                "booking_id": str(booking.id),
                "payment_type": "full",
                "token": "tok_test_456",
                "payment_method_id": "visa",
                "installments": 1,
                "payer_email": contractor_user.email,
            },
        )
    assert res2.status_code == 409
    assert "en proceso" in res2.json()["detail"].lower()


def test_bug5_03_and_fe5_11_booking_link_requires_signature_and_spanish_completeness(
    client, db_session
):
    """BUG5-03 y FE5-11: crear link exige firma del músico y completitud exige paso 9 con etiquetas en español."""
    from app.services.profile_validation import validate_musician_profile

    _, musician_user, musician, _, _ = _setup_users(db_session)
    validation = validate_musician_profile(db_session, musician)
    contract_step = next(step for step in validation.steps if step.id == 9)
    assert contract_step.required is True

    # Al no tener firma ni DNI, missing_fields debe estar íntegramente en español
    assert any("firma" in item.lower() for item in validation.missing_fields)
    for label in validation.missing_fields:
        assert "_" not in label  # No debe exponer claves crudas tipo "signature_image_url"

    _auth_cookie(client, musician_user)
    res_no_sig = client.post(
        "/api/v1/booking-links",
        json={"price": 300.0},
    )
    assert res_no_sig.status_code == 400
    assert "firma" in res_no_sig.json()["detail"].lower()

    musician.signature_image_url = "https://storage.chiv.app/signatures/musician-v4.png"
    db_session.commit()

    res_ok = client.post(
        "/api/v1/booking-links",
        json={"price": 300.0},
    )
    assert res_ok.status_code in (200, 201)


def test_bug5_05_checkout_normalizes_and_rejects_duplicate_document_number(
    client, db_session
):
    """BUG5-05: checkout normaliza document_number y devuelve 409 tanto en usuario nuevo como existente."""
    _, _, musician, _, contractor = _setup_users(db_session)
    musician.signature_image_url = "https://storage.chiv.app/signatures/musician-v4.png"
    contractor.document_number = "44556677A"

    other_existing = User(
        email="existing_nodoc_v5@test.com",
        fullname="Existente Sin DNI",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(other_existing)
    db_session.flush()
    db_session.add(
        ContractorProfile(
            user_id=other_existing.id,
            status=ProfileStatus.published,
        )
    )

    link = BookingLink(
        token="test-token-bug5-05",
        musician_id=musician.id,
        price=Decimal("350.00"),
        status=BookingLinkStatus.active,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db_session.add(link)
    db_session.commit()

    future_str = (now_peru() + timedelta(days=12)).date().isoformat()

    # 1. Usuario nuevo con documento con espacios/minúsculas que colisiona con contractor
    client.cookies.clear()
    res_new_dup = client.post(
        f"/api/v1/booking-links/{link.token}/checkout",
        json={
            "event_date": future_str,
            "start_time": "19:00",
            "event_type": "Boda",
            "location_address": "Av. Arequipa 1200",
            "location_city": "Lima",
            "fullname": "Cliente Nuevo",
            "email": "nuevo_colision_v5@test.com",
            "phone": "988777666",
            "document_number": "  4455 6677a ",
            "terms_accepted": True,
        },
    )
    assert res_new_dup.status_code == 409

    # 2. Usuario existente autenticado que envía un documento perteneciente a otra cuenta
    _auth_cookie(client, other_existing)
    res_existing_dup = client.post(
        f"/api/v1/booking-links/{link.token}/checkout",
        json={
            "event_date": future_str,
            "start_time": "19:00",
            "event_type": "Boda",
            "location_address": "Av. Arequipa 1200",
            "location_city": "Lima",
            "fullname": "Existente Sin DNI",
            "email": other_existing.email,
            "phone": "988777666",
            "document_number": " 4455 6677a ",
            "terms_accepted": True,
        },
    )
    assert res_existing_dup.status_code == 409


def test_fe5_04_unauthenticated_resend_verification_by_email(client, db_session):
    """FE5-04: POST /auth/resend-verification permite reenviar correo sin sesión activa."""
    unverified = User(
        email="resend_nosession_v5@test.com",
        fullname="Sin Sesion",
        role=UserRole.contractor,
        password_hash=hash_password("ClaveSegura!123"),
        is_active=True,
        is_verified=False,
    )
    db_session.add(unverified)
    db_session.commit()

    client.cookies.clear()
    with patch("app.services.email.auth_emails.send_templated_email") as mock_send:
        res = client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "resend_nosession_v5@test.com"},
        )
    assert res.status_code == 200
    assert mock_send.called
    db_session.refresh(unverified)
    assert unverified.email_verification_token is not None

