import pytest

pytestmark = pytest.mark.db


def _register_and_login(client, fullname: str, email: str, username: str | None = None):
    uname = username or fullname.lower().replace(" ", "-")
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "SuperSecreta123!",
            "fullname": fullname,
            "username": uname,
            "role": "musician",
            "phone": None,
            "accepted_terms": True,
        },
    )
    client.post("/api/v1/auth/login", json={"email": email, "password": "SuperSecreta123!"})
    return resp


def test_registered_musician_gets_a_slug(client):
    _register_and_login(client, "Los Mariachis Reales", "reales@example.com", "los-mariachis-reales")
    response = client.get("/api/v1/profiles/musician/me")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "los-mariachis-reales"
    assert body["username"] == "los-mariachis-reales"


def test_colliding_stage_names_rejected_on_registration(client):
    resp1 = _register_and_login(client, "Los Mariachis Reales", "reales1@example.com", "los-mariachis-reales")
    assert resp1.status_code == 201

    resp2 = client.post(
        "/api/v1/auth/register",
        json={
            "email": "reales2@example.com",
            "password": "SuperSecreta123!",
            "fullname": "Los Mariachis Reales",
            "username": "los-mariachis-reales",
            "role": "musician",
            "phone": None,
            "accepted_terms": True,
        },
    )
    assert resp2.status_code == 400
    assert "ya existe un músico" in resp2.json()["detail"].lower()


def test_update_musician_username_and_fullname(client):
    _register_and_login(client, "Mariachi Original", "original@example.com", "mariachi-original")
    update_resp = client.put(
        "/api/v1/profiles/musician",
        json={
            "username": "mariachi-nuevo-slug",
            "fullname": "Nuevo Nombre Completo",
        },
    )
    assert update_resp.status_code == 200
    data = update_resp.json()
    assert data["username"] == "mariachi-nuevo-slug"
    assert data["fullname"] == "Nuevo Nombre Completo"
    assert data["slug"] == "mariachi-nuevo-slug"

    # Verify /me endpoint also returns updated values
    me_resp = client.get("/api/v1/profiles/musician/me")
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["username"] == "mariachi-nuevo-slug"
    assert me_data["fullname"] == "Nuevo Nombre Completo"
    assert me_data["slug"] == "mariachi-nuevo-slug"


def test_update_musician_duplicate_username_fails(client):
    _register_and_login(client, "Mariachi Primero", "primero@example.com", "mariachi-primero")
    _register_and_login(client, "Mariachi Segundo", "segundo@example.com", "mariachi-segundo")

    # Try to change segundo's username to primero's username
    update_resp = client.put(
        "/api/v1/profiles/musician",
        json={"username": "mariachi-primero"},
    )
    assert update_resp.status_code == 400
    assert "nombre de usuario" in update_resp.json()["detail"].lower()
