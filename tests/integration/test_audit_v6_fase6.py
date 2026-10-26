"""Tests de regresión para la Fase 6 del Plan de Cierre (Auditoría v6):
cobertura de ítems previamente sin test dedicado (BUG5-02, m15, SEC5-01, UX5-08, UX-08)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.core.hashing import hash_password
from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.ensemble_member import (
    BookingMemberInvite,
    BookingMemberInviteStatus,
    EnsembleMember,
    EnsembleMemberStatus,
)
from app.models.musician_availability import MusicianAvailability
from app.models.musician_media import MediaType, MusicianMedia
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.oauth_account import OAuthAccount, OAuthProvider
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.contract_pdf import compute_contract_body_sha256
from app.services.oauth import OAuthProfile
from app.services.platform_payment import contractor_payable_total


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    return {"Cookie": f"access_token={token}"}


def test_bug5_02_admin_approve_musician_incomplete_and_complete(client, db_session):
    """BUG5-02: /admin/musicians/{id}/approve rechaza con 400 un perfil incompleto
    y aprueba con 200 (status=published, is_verified=True) cuando cumple todos los pasos obligatorios."""
    admin = User(
        email="admin.bug502.m@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Admin Tester",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    musician_user = User(
        email="musico.bug502@test.com",
        username="mariachi_sol_lima",
        password_hash=hash_password("Pass1234!"),
        fullname="Carlos Mariachi",
        role=UserRole.musician,
        is_active=True,
        is_verified=False,
    )
    db_session.add_all([admin, musician_user])
    db_session.flush()

    profile = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi Sol de Lima",
        bio="Biografía corta",  # < 40 chars -> incompleto
        status=ProfileStatus.pending_review,
    )
    db_session.add(profile)
    db_session.commit()

    headers = _auth_headers(admin)

    # 1. Perfil incompleto -> 400 con detalle de campos faltantes
    r_incomplete = client.post(
        f"/api/v1/admin/musicians/{profile.id}/approve",
        headers=headers,
    )
    assert r_incomplete.status_code == 400, r_incomplete.text
    detail = r_incomplete.json()["detail"]
    assert "El perfil está incompleto" in detail
    assert "Faltan:" in detail

    db_session.refresh(profile)
    db_session.refresh(musician_user)
    assert profile.status == ProfileStatus.pending_review
    assert musician_user.is_verified is False

    # 2. Completar todos los campos obligatorios (pasos 1, 2, 8, 9)
    profile.bio = (
        "Agrupación profesional de mariachi con más de diez años de trayectoria "
        "en eventos corporativos y serenatas en Lima."
    )
    profile.profile_image_url = "https://res.cloudinary.com/demo/image/upload/avatar.jpg"
    profile.genres = ["Mariachi", "Ranchera"]
    profile.instruments = ["Trompeta", "Violín", "Guitarrón"]
    profile.availability_type = AvailabilityType.per_event
    profile.id_document_url = "https://res.cloudinary.com/demo/image/upload/dni.jpg"
    profile.contract_template_body = (
        "<p>Cláusula primera: El músico se compromete a brindar el espectáculo musical "
        "puntualmente en la fecha, hora y dirección pactadas con el cliente.</p>"
    )
    profile.signature_image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    db_session.commit()

    r_complete = client.post(
        f"/api/v1/admin/musicians/{profile.id}/approve",
        headers=headers,
    )
    assert r_complete.status_code == 200, r_complete.text
    data = r_complete.json()
    assert data["status"] == "published"

    db_session.refresh(profile)
    db_session.refresh(musician_user)
    assert profile.status == ProfileStatus.published
    assert profile.published_at is not None
    assert musician_user.is_verified is True


def test_bug5_02_admin_approve_contractor_incomplete_and_complete(client, db_session):
    """BUG5-02: /admin/contractors/{id}/approve rechaza con 400 un perfil incompleto
    y aprueba con 200 cuando todos los datos personales y documento están completos."""
    admin = User(
        email="admin.bug502.c@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Admin Tester",
        role=UserRole.admin,
        is_active=True,
        is_verified=True,
    )
    contractor_user = User(
        email="cliente.bug502@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Lucía Contratista",
        role=UserRole.contractor,
        is_active=True,
        is_verified=False,
    )
    db_session.add_all([admin, contractor_user])
    db_session.flush()

    c_profile = ContractorProfile(
        user_id=contractor_user.id,
        city="Lima",
        status=ProfileStatus.pending_review,
    )
    db_session.add(c_profile)
    db_session.commit()

    headers = _auth_headers(admin)

    # 1. Perfil incompleto (faltan documento, dirección e imagen de DNI) -> 400
    r_incomplete = client.post(
        f"/api/v1/admin/contractors/{c_profile.id}/approve",
        headers=headers,
    )
    assert r_incomplete.status_code == 400, r_incomplete.text
    detail = r_incomplete.json()["detail"]
    assert "El perfil está incompleto" in detail
    assert "Faltan:" in detail

    db_session.refresh(c_profile)
    db_session.refresh(contractor_user)
    assert c_profile.status == ProfileStatus.pending_review
    assert contractor_user.is_verified is False

    # 2. Completar pasos obligatorios (datos personales + documento de identidad)
    c_profile.document_type = "DNI"
    c_profile.document_number = "45678912"
    c_profile.address = "Av. Arequipa 1234, Miraflores"
    c_profile.city = "Lima"
    c_profile.id_document_url = "https://res.cloudinary.com/demo/image/upload/dni_cliente.jpg"
    db_session.commit()

    r_complete = client.post(
        f"/api/v1/admin/contractors/{c_profile.id}/approve",
        headers=headers,
    )
    assert r_complete.status_code == 200, r_complete.text
    assert r_complete.json()["status"] == "published"

    db_session.refresh(c_profile)
    db_session.refresh(contractor_user)
    assert c_profile.status == ProfileStatus.published
    assert contractor_user.is_verified is True


def test_m15_contract_body_sha256_enforced_in_preference_and_process(
    client, db_session, monkeypatch
):
    """m15: Al firmar en /mercadopago/preference o /mercadopago/process:
    - sin contract_body_sha256 devuelve 422;
    - con hash distinto al del cuerpo actual devuelve 409;
    - con el hash exacto firma el contrato y guarda __signed_body_sha256__."""
    monkeypatch.setattr("app.core.config.settings.MERCADO_PAGO_ACCESS_TOKEN", "TEST-token")
    monkeypatch.setattr("app.core.config.settings.MERCADO_PAGO_PUBLIC_KEY", "TEST-pub")

    musician_user = User(
        email="musico.m15@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Músico M15",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    contractor_user = User(
        email="cliente.m15@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Cliente M15",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
    )
    db_session.add_all([musician_user, contractor_user])
    db_session.flush()

    m_prof = MusicianProfile(
        user_id=musician_user.id,
        stage_name="Mariachi M15",
        status=ProfileStatus.published,
        price_per_event=Decimal("500.00"),
    )
    c_prof = ContractorProfile(
        user_id=contractor_user.id,
        status=ProfileStatus.published,
        document_type="DNI",
        document_number="41234567",
        address="Av. Larco 500",
        city="Lima",
    )
    db_session.add_all([m_prof, c_prof])
    db_session.flush()

    booking = Booking(
        contractor_id=c_prof.id,
        musician_id=m_prof.id,
        event_type="Aniversario",
        event_date=date.today() + timedelta(days=12),
        start_time=time(20, 0),
        end_time=time(22, 0),
        location_city="Lima",
        location_address="Av. Larco 500",
        price_agreed=Decimal("500.00"),
        status=BookingStatus.contract_pending,
    )
    db_session.add(booking)
    db_session.flush()

    contract_title = "Contrato M15"
    contract_html = (
        "<h2>Contrato de Servicios Musicales</h2>"
        "<p>Las partes acuerdan el servicio musical por S/ 500.00 en la fecha indicada.</p>"
    )
    expected_sha = compute_contract_body_sha256(contract_title, contract_html)
    wrong_sha = "a" * 64

    contract = Contract(
        booking_id=booking.id,
        title=contract_title,
        body=contract_html,
        musician_signed=True,
        musician_sign_timestamp=datetime.utcnow(),
        context={},
    )
    db_session.add(contract)
    db_session.commit()

    headers = _auth_headers(contractor_user)
    sig_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    payable = float(contractor_payable_total(booking))

    # 1. /preference sin contract_body_sha256 -> 422
    r_pref_missing = client.post(
        "/api/v1/payments/mercadopago/preference",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "signature_image_url": sig_data_url,
            "accepted_terms": True,
        },
    )
    assert r_pref_missing.status_code == 422, r_pref_missing.text

    # 2. /preference con hash incorrecto -> 409
    r_pref_wrong = client.post(
        "/api/v1/payments/mercadopago/preference",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "payment_type": "full",
            "signature_image_url": sig_data_url,
            "contract_body_sha256": wrong_sha,
            "accepted_terms": True,
        },
    )
    assert r_pref_wrong.status_code == 409, r_pref_wrong.text
    assert "cambió" in r_pref_wrong.json()["detail"]

    # 3. /process sin contract_body_sha256 -> 422
    r_proc_missing = client.post(
        "/api/v1/payments/mercadopago/process",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "token": "tok_test_123",
            "payment_method_id": "visa",
            "payment_type": "full",
            "amount": payable,
            "signature_image_url": sig_data_url,
            "accepted_terms": True,
        },
    )
    assert r_proc_missing.status_code == 422, r_proc_missing.text

    # 4. /process con hash incorrecto -> 409
    r_proc_wrong = client.post(
        "/api/v1/payments/mercadopago/process",
        headers=headers,
        json={
            "booking_id": str(booking.id),
            "token": "tok_test_123",
            "payment_method_id": "visa",
            "payment_type": "full",
            "amount": payable,
            "signature_image_url": sig_data_url,
            "contract_body_sha256": wrong_sha,
            "accepted_terms": True,
        },
    )
    assert r_proc_wrong.status_code == 409, r_proc_wrong.text

    # 5. /preference con hash exacto -> 200 y guarda __signed_body_sha256__
    with patch(
        "app.services.mercadopago_service.create_preference",
        return_value={
            "preference_id": "pref-m15-ok",
            "init_point": "https://www.mercadopago.com.pe/checkout/v1/redirect?pref_id=pref-m15-ok",
            "sandbox_init_point": None,
            "public_key": "TEST-pub",
            "amount": payable,
            "currency": "PEN",
            "payment_type": "full",
        },
    ):
        r_pref_ok = client.post(
            "/api/v1/payments/mercadopago/preference",
            headers=headers,
            json={
                "booking_id": str(booking.id),
                "payment_type": "full",
                "signature_image_url": sig_data_url,
                "contract_body_sha256": expected_sha,
                "accepted_terms": True,
            },
        )
    assert r_pref_ok.status_code == 200, r_pref_ok.text
    db_session.refresh(contract)
    assert contract.contractor_signed is True
    assert contract.context.get("__signed_body_sha256__") == expected_sha


def test_sec5_01_google_oauth_link_requires_matching_verified_email(client, db_session):
    """SEC5-01: Si una cuenta aún no verificó su correo (email_verified_at is None),
    vincular una cuenta de Google con otro correo o correo sin verificar falla con 400;
    vincular el mismo correo verificado de Google marca email_verified_at, revoca sesiones
    previas y crea el vínculo OAuthAccount."""
    from unittest.mock import AsyncMock

    user = User(
        email="titular.sec501@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Titular SEC501",
        role=UserRole.contractor,
        is_active=True,
        is_verified=False,
        email_verified_at=None,
    )
    db_session.add(user)
    db_session.commit()

    tok = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        version=user.token_version or 0,
    )
    client.cookies.set("access_token", tok)

    # 1. Correo distinto en cuenta sin verificar -> 400 y NO marca email_verified_at
    with patch(
        "app.api.v1.endpoints.auth.verify_google_id_token",
        new=AsyncMock(
            return_value=OAuthProfile(
                provider=OAuthProvider.google,
                provider_user_id="google-sub-other",
                email="otro.correo@test.com",
                fullname="Otro Usuario",
                picture_url=None,
                email_verified=True,
            )
        ),
    ):
        r_diff = client.post(
            "/api/v1/auth/oauth/google/credential",
            json={"credential": "fake.jwt.token", "intent": "link"},
        )
    assert r_diff.status_code == 400, r_diff.text
    assert "verificar tu correo" in r_diff.json()["detail"]
    db_session.refresh(user)
    assert user.email_verified_at is None
    assert (
        db_session.query(OAuthAccount)
        .filter(OAuthAccount.user_id == user.id)
        .count()
        == 0
    )

    # 2. Correo igual pero no verificado en Google -> 400
    with patch(
        "app.api.v1.endpoints.auth.verify_google_id_token",
        new=AsyncMock(
            return_value=OAuthProfile(
                provider=OAuthProvider.google,
                provider_user_id="google-sub-unverified",
                email="titular.sec501@test.com",
                fullname="Titular SEC501",
                picture_url=None,
                email_verified=False,
            )
        ),
    ):
        r_unverified = client.post(
            "/api/v1/auth/oauth/google/credential",
            json={"credential": "fake.jwt.token", "intent": "link"},
        )
    assert r_unverified.status_code == 400, r_unverified.text
    db_session.refresh(user)
    assert user.email_verified_at is None

    # 3. Correo igual y verificado -> 200 (status='linked'), verifica correo y vincula OAuthAccount
    with patch(
        "app.api.v1.endpoints.auth.verify_google_id_token",
        new=AsyncMock(
            return_value=OAuthProfile(
                provider=OAuthProvider.google,
                provider_user_id="google-sub-titular-ok",
                email="titular.sec501@test.com",
                fullname="Titular SEC501",
                picture_url=None,
                email_verified=True,
            )
        ),
    ):
        r_ok = client.post(
            "/api/v1/auth/oauth/google/credential",
            json={"credential": "fake.jwt.token", "intent": "link"},
        )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["status"] == "linked"
    db_session.refresh(user)
    assert user.email_verified_at is not None
    assert user.is_verified is True
    linked = (
        db_session.query(OAuthAccount)
        .filter(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider == OAuthProvider.google,
        )
        .first()
    )
    assert linked is not None
    assert linked.provider_user_id == "google-sub-titular-ok"
    client.cookies.clear()


def test_ux5_08_and_ux08_calls_enrichment_and_musician_date_filter(client, db_session):
    """UX5-08: /musician/my-calls incluye event_type, event_date, start_time,
    location_city, location_address y leader_name.
    UX-08: /musicians/search con available_date filtra según los horarios del músico."""
    leader_user = User(
        email="lider.ux508@test.com",
        username="mariachi_lider_ux",
        password_hash=hash_password("Pass1234!"),
        fullname="Líder Mariachi UX",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    other_musician_user = User(
        email="otro.ux08@test.com",
        username="mariachi_otro_ux",
        password_hash=hash_password("Pass1234!"),
        fullname="Otro Mariachi UX",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    member_user = User(
        email="integrante.ux508@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Integrante Trompeta",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
    )
    contractor_user = User(
        email="cliente.ux508@test.com",
        password_hash=hash_password("Pass1234!"),
        fullname="Cliente UX",
        role=UserRole.contractor,
        is_active=True,
        is_verified=True,
    )
    db_session.add_all([leader_user, other_musician_user, member_user, contractor_user])
    db_session.flush()

    def _make_public_musician(user_obj: User, stage_name: str, slug: str) -> MusicianProfile:
        prof = MusicianProfile(
            user_id=user_obj.id,
            stage_name=stage_name,
            slug=slug,
            bio="Agrupación de mariachi profesional con amplio repertorio para serenatas y bodas en Lima.",
            profile_image_url="https://res.cloudinary.com/demo/image/upload/p.jpg",
            genres=["Mariachi"],
            instruments=["Trompeta", "Violín"],
            availability_type=AvailabilityType.per_event,
            location_city="Lima",
            location_zone="Miraflores",
            price_per_event=Decimal("450.00"),
            songs=["El Rey", "Si nos dejan"],
            videos=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
            instagram_url="https://instagram.com/mariachi",
            id_document_url="https://res.cloudinary.com/demo/image/upload/dni.jpg",
            contract_template_body=(
                "<p>Contrato estándar con más de cincuenta caracteres de texto real para validación.</p>"
            ),
            signature_image_url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            status=ProfileStatus.published,
        )
        db_session.add(prof)
        db_session.flush()
        db_session.add(
            MusicianMedia(
                musician_id=prof.id,
                type=MediaType.image,
                url="https://res.cloudinary.com/demo/image/upload/g1.jpg",
                order_index=0,
            )
        )
        return prof

    leader_prof = _make_public_musician(leader_user, "Mariachi Líder UX", "mariachi-lider-ux")
    other_prof = _make_public_musician(other_musician_user, "Mariachi Otro UX", "mariachi-otro-ux")

    # Elegir una fecha futura y configurar slots distintos por día de la semana (0=Domingo .. 6=Sábado)
    from app.services.availability_match import to_stored_day_of_week

    target_date = date.today() + timedelta(days=14)
    target_dow = to_stored_day_of_week(target_date)
    other_dow = (target_dow + 2) % 7

    db_session.add_all(
        [
            MusicianAvailability(
                musician_id=leader_prof.id,
                day_of_week=target_dow,
                start_time=time(18, 0),
                end_time=time(23, 0),
            ),
            MusicianAvailability(
                musician_id=other_prof.id,
                day_of_week=other_dow,
                start_time=time(18, 0),
                end_time=time(23, 0),
            ),
        ]
    )

    c_prof = ContractorProfile(
        user_id=contractor_user.id,
        status=ProfileStatus.published,
    )
    db_session.add(c_prof)
    db_session.flush()

    booking = Booking(
        contractor_id=c_prof.id,
        musician_id=leader_prof.id,
        event_type="Boda Civil",
        event_date=target_date,
        start_time=time(19, 30),
        end_time=time(21, 30),
        location_city="Lima",
        location_address="Av. El Golf 800, San Isidro",
        price_agreed=Decimal("450.00"),
        status=BookingStatus.payment_retained,
    )
    db_session.add(booking)
    db_session.flush()

    ens_member = EnsembleMember(
        leader_user_id=leader_user.id,
        member_user_id=member_user.id,
        fullname=member_user.fullname,
        email=member_user.email,
        specialties=["Trompeta"],
        status=EnsembleMemberStatus.active,
    )
    db_session.add(ens_member)
    db_session.flush()

    invite = BookingMemberInvite(
        booking_id=booking.id,
        ensemble_member_id=ens_member.id,
        status=BookingMemberInviteStatus.pending,
        response_token="tok-ux508-hash",
    )
    db_session.add(invite)
    db_session.commit()

    # 1. UX5-08: /musician/my-calls devuelve los datos completos del evento y líder
    r_calls = client.get(
        "/api/v1/musician/my-calls",
        headers=_auth_headers(member_user),
    )
    assert r_calls.status_code == 200, r_calls.text
    calls = r_calls.json()
    assert len(calls) == 1
    call = calls[0]
    assert call["event_type"] == "Boda Civil"
    assert call["event_date"] == target_date.isoformat()
    assert call["start_time"].startswith("19:30")
    assert call["location_city"] == "Lima"
    assert call["location_address"] == "Av. El Golf 800, San Isidro"
    assert call["leader_name"] == "Líder Mariachi UX"

    # 2. UX-08: /musicians/search con available_date incluye a leader_prof y excluye a other_prof
    r_search = client.post(
        "/api/v1/musicians/search",
        json={"city": "Lima", "available_date": target_date.isoformat()},
    )
    assert r_search.status_code == 200, r_search.text
    found_ids = {item["musician_id"] for item in r_search.json()}
    assert str(leader_prof.id) in found_ids
    assert str(other_prof.id) not in found_ids
