"""Tests de integración para Fase 4 — Producto, Legal y SEO (WP-18 a WP-24)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import uuid
from zoneinfo import ZoneInfo

from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.notification import Notification
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def _make_verified_contractor(db_session):
    user = User(
        email=f"contractor-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("ContractorPass123!"),
        fullname="Contratista Fase 4",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()
    profile = ContractorProfile(
        user_id=user.id,
        city="Lima",
        address="Av. Larco 100",
        document_type="DNI",
        document_number="12345678",
        id_document_url="/api/v1/uploads/private/id-doc.pdf",
        status=ProfileStatus.published,
    )
    db_session.add(profile)
    db_session.commit()
    return user, profile


def _make_verified_musician(db_session, stage_name: str, city: str = "Lima"):
    user = User(
        email=f"musician-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("MusicianPass123!"),
        fullname=stage_name,
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()
    profile = MusicianProfile(
        user_id=user.id,
        stage_name=stage_name,
        slug=f"{stage_name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        bio="Agrupación verificada para eventos.",
        genres=["Rock", "Pop"],
        instruments=["Guitarra", "Voz"],
        price_per_hour=Decimal("400.00"),
        price_per_event=Decimal("1200.00"),
        location_city=city,
        profile_image_url="https://example.com/profile.jpg",
        id_document_url="/api/v1/uploads/private/musician-id.pdf",
        status=ProfileStatus.published,
    )
    db_session.add(profile)
    db_session.commit()
    return user, profile


def test_wp21_booking_optional_address_with_city_and_batch_quotes(client, db_session):
    """WP-21 & D9: Permite cotizar indicando solo ciudad + campos estructurados y pedir cotización en lote."""
    contractor_user, _ = _make_verified_contractor(db_session)
    _, musician1 = _make_verified_musician(db_session, "Banda Uno", city="Lima")
    _, musician2 = _make_verified_musician(db_session, "Banda Dos", city="Lima")

    headers = _auth_headers(contractor_user)
    future_date = (date.today() + timedelta(days=20)).isoformat()

    # 1. Crear solicitud sin location_address exacto, solo con location_city y campos de contexto
    res_single = client.post(
        "/api/v1/bookings",
        headers=headers,
        json={
            "musician_id": str(musician1.id),
            "event_date": future_date,
            "start_time": "19:00:00",
            "location_address": "",
            "location_city": "Miraflores, Lima",
            "event_type": "Matrimonio",
            "guests_count": 120,
            "duration_hours": 3,
            "estimated_budget": "1500.00",
        },
    )
    assert res_single.status_code == 201, res_single.text
    body = res_single.json()
    assert body["location_address"] == "Por definir en Miraflores, Lima"
    assert "Invitados aprox.: 120" in (body["event_description"] or "")
    assert "Duración estimada: 3 h" in (body["event_description"] or "")

    # 2. Solicitud en lote (D9: múltiples artistas a la vez)
    future_date_batch = (date.today() + timedelta(days=25)).isoformat()
    res_batch = client.post(
        "/api/v1/bookings/batch",
        headers=headers,
        json={
            "musician_ids": [str(musician1.id), str(musician2.id)],
            "event_date": future_date_batch,
            "start_time": "20:00:00",
            "location_city": "San Isidro, Lima",
            "event_type": "Evento corporativo",
            "guests_count": 80,
            "duration_hours": 2,
        },
    )
    assert res_batch.status_code == 201, res_batch.text
    batch_items = res_batch.json()
    assert len(batch_items) == 2
    created_musician_ids = {item["musician_id"] for item in batch_items}
    assert created_musician_ids == {str(musician1.id), str(musician2.id)}


def test_multiple_quote_is_all_or_nothing(client, db_session):
    """Cotización múltiple: si un artista no puede recibir la solicitud, no se crea ninguna."""
    contractor_user, _ = _make_verified_contractor(db_session)
    _, musician1 = _make_verified_musician(db_session, "Banda Tres", city="Lima")
    headers = _auth_headers(contractor_user)
    event_date = (date.today() + timedelta(days=30)).isoformat()

    res = client.post(
        "/api/v1/bookings/batch",
        headers=headers,
        json={
            "musician_ids": [str(musician1.id), str(uuid.uuid4())],  # el segundo no existe
            "event_date": event_date,
            "start_time": "20:00:00",
            "location_city": "Lima",
            "event_type": "Cumpleaños",
        },
    )
    assert res.status_code == 404, res.text
    assert "No se envió ninguna solicitud" in res.json()["detail"]
    db_session.expire_all()
    assert db_session.query(Booking).filter(Booking.musician_id == musician1.id).count() == 0
    assert db_session.query(Notification).filter(Notification.type == "booking_requested").count() == 0

    # Con artistas válidos, se envían todas y cada artista recibe su aviso.
    _, musician2 = _make_verified_musician(db_session, "Banda Cuatro", city="Lima")
    ok = client.post(
        "/api/v1/bookings/batch",
        headers=headers,
        json={
            "musician_ids": [str(musician1.id), str(musician2.id)],
            "event_date": event_date,
            "start_time": "20:00:00",
            "location_city": "Lima",
            "event_type": "Cumpleaños",
        },
    )
    assert ok.status_code == 201, ok.text
    assert len(ok.json()) == 2
