"""Regresiones de seguridad de la fase 0 (toma de cuentas y pagos)."""

from app.core.jwt import create_magic_token


def _guest(client, email="guest@example.com", **extra):
    return client.post(
        "/api/v1/auth/guest-register",
        json={"email": email, "fullname": "Invitado", "phone": "999111222", **extra},
    )


def test_guest_register_ignores_requested_role(client):
    res = _guest(client, email="intruso@example.com", role="admin")
    assert res.status_code == 201
    assert res.json()["role"] == "contractor"


def test_guest_register_existing_email_does_not_log_in(client):
    assert _guest(client, email="victima@example.com").status_code == 201
    client.cookies.clear()

    res = _guest(client, email="victima@example.com", phone="999333444")
    assert res.status_code == 409
    assert "access_token" not in res.cookies
    assert client.get("/api/v1/auth/me").status_code == 401


def test_magic_link_send_endpoint_removed(client):
    res = client.post("/api/v1/auth/magic-link/send", json={"email": "x@example.com"})
    assert res.status_code in (404, 405)


def test_magic_token_is_not_a_session_cookie(client):
    created = _guest(client, email="magic@example.com").json()
    client.cookies.clear()

    client.cookies.set("access_token", create_magic_token(subject=created["id"], redirect_to="/"))
    assert client.get("/api/v1/auth/me").status_code == 401


def test_magic_link_login_blocks_open_redirect(client):
    created = _guest(client, email="redirect@example.com").json()
    client.cookies.clear()

    token = create_magic_token(subject=created["id"], redirect_to="@evil.com")
    res = client.get(f"/api/v1/auth/magic-link/login?token={token}", follow_redirects=False)
    assert res.status_code in (302, 307)
    location = res.headers["location"]
    assert "evil.com" not in location
    assert location.endswith("/")


def test_contractor_cannot_retain_or_release_payment(client):
    assert _guest(client, email="pagador@example.com").status_code == 201
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/api/v1/payments/{fake_id}/retain").status_code == 403
    assert client.post(f"/api/v1/payments/{fake_id}/release").status_code == 403
