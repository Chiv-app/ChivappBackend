"""Alta guiada del músico: usuario automático, contrato por defecto y envío a revisión."""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.contract_pdf import contract_html_text_length

pytestmark = pytest.mark.db


def _musician(db: Session, *, username: str | None = None) -> tuple[User, MusicianProfile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"musico_{suffix}@test.com",
        fullname="Carlos Quispe Rivera",
        role=UserRole.musician,
        username=username,
        is_active=True,
        email_verified_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    profile = MusicianProfile(user_id=user.id, status=ProfileStatus.draft, availability_type=AvailabilityType.both)
    db.add(profile)
    db.commit()
    return user, profile


def _login(client: TestClient, user: User) -> None:
    client.cookies.clear()
    client.cookies.set(
        "access_token", create_access_token(subject=str(user.id), role="musician", version=user.token_version or 0)
    )


def test_setting_the_stage_name_creates_the_username_and_slug_automatically(client: TestClient, db_session: Session):
    user, profile = _musician(db_session)
    _login(client, user)

    res = client.put("/api/v1/profiles/musician", json={"stage_name": "Los Hermanos Quispe"})
    assert res.status_code == 200, res.text

    db_session.expire_all()
    assert db_session.get(User, user.id).username == "los-hermanos-quispe"
    assert db_session.get(MusicianProfile, profile.id).slug == "los-hermanos-quispe"


def test_automatic_username_never_collides_with_an_existing_one(client: TestClient, db_session: Session):
    _musician(db_session, username="los-hermanos-quispe")
    user, profile = _musician(db_session)
    _login(client, user)

    res = client.put("/api/v1/profiles/musician", json={"stage_name": "Los Hermanos Quispe"})
    assert res.status_code == 200, res.text

    db_session.expire_all()
    assert db_session.get(User, user.id).username == "los-hermanos-quispe-2"
    assert db_session.get(MusicianProfile, profile.id).slug == "los-hermanos-quispe-2"


def test_a_username_chosen_by_the_musician_is_kept(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, username="carlos-musico")
    _login(client, user)

    res = client.put("/api/v1/profiles/musician", json={"stage_name": "Otro Nombre Artístico"})
    assert res.status_code == 200, res.text

    db_session.expire_all()
    assert db_session.get(User, user.id).username == "carlos-musico"


def test_a_new_musician_account_starts_with_the_default_contract(client: TestClient, db_session: Session):
    email = f"nuevo_{uuid.uuid4().hex[:8]}@test.com"
    res = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "ClaveSegura-123!", "role": "musician", "accepted_terms": True},
    )
    assert res.status_code in (200, 201), res.text

    user = db_session.query(User).filter(User.email == email).one()
    profile = db_session.query(MusicianProfile).filter(MusicianProfile.user_id == user.id).one()
    assert contract_html_text_length(profile.contract_template_body) >= 50
    assert profile.contract_template_title


def test_submitting_without_a_custom_contract_uses_the_default_one(client: TestClient, db_session: Session):
    user, profile = _musician(db_session, username=f"musico-{uuid.uuid4().hex[:6]}")
    profile.stage_name = f"Grupo Aurora {uuid.uuid4().hex[:6]}"
    profile.slug = user.username
    profile.bio = "Soy un grupo de Arequipa que anima celebraciones con música para toda la familia."
    profile.profile_image_url = "/uploads/foto.jpg"
    profile.genres = ["Cumbia", "Salsa"]
    profile.instruments = ["Voz", "Teclado"]
    profile.location_city = "Arequipa"
    profile.price_per_event = Decimal("450.00")
    profile.id_document_url = "/uploads/private/dni.jpg"
    profile.signature_image_url = "/uploads/private/firma.png"
    profile.contract_template_body = None  # sin contrato propio
    db_session.commit()
    _login(client, user)

    res = client.post("/api/v1/profiles/musician/submit")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "pending_review"

    db_session.expire_all()
    saved = db_session.get(MusicianProfile, profile.id)
    assert contract_html_text_length(saved.contract_template_body) >= 50
