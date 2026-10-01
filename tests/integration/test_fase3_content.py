"""Regresiones de la fase 3: subidas seguras, archivos privados, HTML sanitizado."""

from datetime import date, time

import pytest

from app.core.jwt import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.user import User, UserRole
from app.services.contract_pdf import _deny_external_resources
from app.services.email.renderer import render_template

pytestmark = pytest.mark.db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n"


def _user(db, email, role=UserRole.contractor):
    user = User(email=email, fullname=email.split("@")[0], role=role)
    db.add(user)
    db.commit()
    return user


def _login(client, user):
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(str(user.id), user.role.value, version=user.token_version or 0))


def _upload(client, content, name="f.png", ctype="image/png", private=False):
    return client.post(
        f"/api/v1/uploads/?private={'true' if private else 'false'}",
        files={"file": (name, content, ctype)},
    )


def test_html_disguised_as_image_is_rejected(client, db_session):
    _login(client, _user(db_session, "a@example.com"))
    res = _upload(client, b"<html><script>alert(1)</script></html>", name="x.png", ctype="image/png")
    assert res.status_code == 400
    res = _upload(client, b"<svg onload=alert(1)>", name="x.svg", ctype="image/svg+xml")
    assert res.status_code == 400


def test_server_chooses_extension_from_content(client, db_session):
    _login(client, _user(db_session, "b@example.com"))
    res = _upload(client, PNG, name="foto.html", ctype="text/html")
    assert res.status_code == 200
    assert res.json()["url"].endswith(".png")


def test_private_file_access_rules(client, db_session):
    owner = _user(db_session, "owner@example.com", UserRole.musician)
    stranger = _user(db_session, "stranger@example.com")
    counterpart = _user(db_session, "client@example.com")
    admin = _user(db_session, "admin@example.com", UserRole.admin)

    musician = MusicianProfile(user_id=owner.id, stage_name="Mariachi")
    contractor = ContractorProfile(user_id=counterpart.id)
    db_session.add_all([musician, contractor])
    db_session.flush()
    db_session.add(
        Booking(
            contractor_id=contractor.id,
            musician_id=musician.id,
            event_date=date(2030, 1, 1),
            start_time=time(20, 0),
            location_address="Av. 1",
            event_type="Boda",
            status=BookingStatus.contract_pending,
        )
    )
    db_session.commit()

    _login(client, owner)
    res = _upload(client, PDF, name="dni.pdf", ctype="application/pdf", private=True)
    assert res.status_code == 200
    url = res.json()["url"]
    assert url.startswith("/uploads/private/")

    assert client.get(url).status_code == 200  # dueño
    _login(client, stranger)
    assert client.get(url).status_code == 404  # sin relación
    _login(client, counterpart)
    assert client.get(url).status_code == 200  # contraparte de una reserva
    _login(client, admin)
    res = client.get(url)
    assert res.status_code == 200
    assert res.headers["x-content-type-options"] == "nosniff"
    client.cookies.clear()
    assert client.get(url).status_code == 401  # anónimo


def test_private_files_are_not_served_statically(client, db_session):
    _login(client, _user(db_session, "c@example.com"))
    url = _upload(client, PDF, name="x.pdf", ctype="application/pdf", private=True).json()["url"]
    name = url.rsplit("/", 1)[-1]
    client.cookies.clear()
    assert client.get(f"/uploads/{name}").status_code == 404


def test_contract_template_is_sanitized_on_save(client, db_session):
    owner = _user(db_session, "musico@example.com", UserRole.musician)
    db_session.add(MusicianProfile(user_id=owner.id, stage_name="Banda"))
    db_session.commit()
    _login(client, owner)
    res = client.put(
        "/api/v1/profiles/musician",
        json={"contract_template_body": '<p>Hola</p><img src=x onerror="alert(1)"><script>alert(2)</script>'},
    )
    assert res.status_code == 200, res.text
    body = res.json()["contract_template_body"]
    assert "onerror" not in body and "<script" not in body
    assert "<p>Hola</p>" in body


def test_pdf_never_fetches_external_resources():
    assert _deny_external_resources("http://169.254.169.254/latest/meta-data", "") == ""
    assert _deny_external_resources("file:///etc/passwd", "") == ""
    assert _deny_external_resources("data:image/png;base64,AAAA", "").startswith("data:")


def test_email_html_escapes_user_values():
    out = render_template("<p>Hola {{name}}</p>", {"name": '<a href="https://evil">clic</a>'}, escape_html=True)
    assert "<a " not in out
    assert "&lt;a href=" in out
