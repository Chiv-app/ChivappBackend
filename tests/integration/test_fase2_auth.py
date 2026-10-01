"""Regresiones de la fase 2: sesiones revocables, state OAuth y IP del cliente."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.jwt import create_access_token, create_signed_state
from app.core.limiter import get_client_ip
from app.models.user import User, UserRole

pytestmark = pytest.mark.db

PASSWORD = "Password123!"


def _register_and_login(client, email="sesion@example.com"):
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "role": "musician", "accepted_terms": True},
    )
    client.cookies.clear()
    res = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200
    return res.cookies["access_token"]


def test_password_change_revokes_other_sessions_but_keeps_current(client):
    old_cookie = _register_and_login(client)

    res = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "OtraClave456#"},
    )
    assert res.status_code == 200
    # El dispositivo actual recibió una cookie nueva y sigue dentro.
    assert client.get("/api/v1/auth/me").status_code == 200

    # Una sesión robada/anterior queda invalidada.
    client.cookies.clear()
    client.cookies.set("access_token", old_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_all_invalidates_session(client):
    cookie = _register_and_login(client, email="todos@example.com")
    assert client.post("/api/v1/auth/logout-all").status_code == 200
    client.cookies.set("access_token", cookie)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_session_token_with_stale_version_rejected(client, db_session):
    user = User(email="ver@example.com", fullname="Ver", role=UserRole.contractor, token_version=3)
    db_session.add(user)
    db_session.commit()
    client.cookies.set("access_token", create_access_token(str(user.id), "contractor", version=2))
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.set("access_token", create_access_token(str(user.id), "contractor", version=3))
    assert client.get("/api/v1/auth/me").status_code == 200


def test_oauth_state_token_cannot_be_used_as_session(client, db_session):
    user = User(email="state@example.com", fullname="State", role=UserRole.contractor)
    db_session.add(user)
    db_session.commit()
    client.cookies.set("access_token", create_signed_state("oauth_state", {"sub": str(user.id)}))
    assert client.get("/api/v1/auth/me").status_code == 401


def test_oauth_callback_without_state_cookie_is_rejected(client):
    state = create_signed_state("oauth_state", {"intent": "login", "nonce": "abc"})
    with patch("app.api.v1.endpoints.auth.exchange_code_for_profile") as exchange:
        res = client.get(
            f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
            follow_redirects=False,
        )
    assert res.status_code in (302, 307)
    assert "invalid_state" in res.headers["location"]
    exchange.assert_not_called()


def test_oauth_callback_with_forged_state_nonce_is_rejected(client):
    state = create_signed_state("oauth_state", {"intent": "login", "nonce": "real"})
    client.cookies.set("oauth_state", "otro")
    with patch("app.api.v1.endpoints.auth.exchange_code_for_profile") as exchange:
        res = client.get(
            f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
            follow_redirects=False,
        )
    assert "invalid_state" in res.headers["location"]
    exchange.assert_not_called()


def test_calendar_callback_rejects_state_of_another_user(client, db_session):
    _register_and_login(client, email="calendar@example.com")
    foreign_state = create_signed_state("calendar_state", {"uid": "00000000-0000-0000-0000-000000000000"})
    with patch("app.api.v1.endpoints.calendar_auth.httpx.post") as post:
        res = client.post(f"/api/v1/calendar-auth/callback?code=x&state={foreign_state}")
    assert res.status_code == 400
    post.assert_not_called()


def _request(headers: dict, host="10.0.0.1"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_client_ip_ignores_spoofed_forwarded_for():
    req = _request({"x-forwarded-for": "1.1.1.1, 203.0.113.9"})
    assert get_client_ip(req) == "203.0.113.9"


def test_client_ip_trusts_proxy_header_only_with_secret():
    headers = {
        "x-forwarded-for": "203.0.113.9",
        "x-chivapp-client-ip": "198.51.100.7",
        "x-chivapp-proxy-secret": "s3cret",
    }
    with patch("app.core.limiter.settings.PROXY_SHARED_SECRET", "s3cret"):
        assert get_client_ip(_request(headers)) == "198.51.100.7"
    with patch("app.core.limiter.settings.PROXY_SHARED_SECRET", "otro"):
        assert get_client_ip(_request(headers)) == "203.0.113.9"
    with patch("app.core.limiter.settings.PROXY_SHARED_SECRET", ""):
        assert get_client_ip(_request(headers)) == "203.0.113.9"
