import pytest

pytestmark = pytest.mark.db


def _register_payload(**overrides):
    payload = {
        "email": "mariachi.test@example.com",
        "password": "SuperSecreta123!",
        "fullname": "Mariachi de Prueba",
        "username": "mariachi-prueba",
        "role": "musician",
        "phone": "+51999888777",
        "accepted_terms": True,
    }
    payload.update(overrides)
    return payload


def test_register_creates_user_and_returns_201(client):
    response = client.post("/api/v1/auth/register", json={
        "email": "mariachi.test@example.com",
        "password": "SuperSecreta123!",
        "role": "musician",
        "accepted_terms": True,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "mariachi.test@example.com"
    assert body["role"] == "musician"


def test_register_musician_does_not_require_username_at_register(client):
    response = client.post("/api/v1/auth/register", json={
        "email": "nuevo.musico@example.com",
        "password": "SuperSecreta123!",
        "role": "musician",
        "accepted_terms": True,
    })
    assert response.status_code == 201
    assert response.json()["username"] is None


def test_register_duplicate_username_returns_400(client):
    client.post("/api/v1/auth/register", json=_register_payload())
    response = client.post(
        "/api/v1/auth/register",
        json=_register_payload(
            email="otro@example.com",
            fullname="Otro Mariachi",
            username="mariachi-prueba",
            phone="+51999888778",
        ),
    )
    assert response.status_code == 400
    assert "nombre de usuario" in response.json()["detail"].lower()


def test_register_duplicate_fullname_returns_400(client):
    client.post("/api/v1/auth/register", json=_register_payload())
    response = client.post(
        "/api/v1/auth/register",
        json=_register_payload(
            email="otro2@example.com",
            fullname="Mariachi de Prueba",
            username="mariachi-prueba-2",
            phone="+51999888779",
        ),
    )
    assert response.status_code == 400
    assert "nombre" in response.json()["detail"].lower()


def test_register_duplicate_email_returns_400(client):
    client.post("/api/v1/auth/register", json=_register_payload())
    response = client.post(
        "/api/v1/auth/register",
        json=_register_payload(username="mariachi-distinto"),
    )
    assert response.status_code == 400


def test_login_with_correct_credentials_returns_token(client):
    client.post("/api/v1/auth/register", json=_register_payload())
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "mariachi.test@example.com", "password": "SuperSecreta123!"},
    )
    assert response.status_code == 200
    assert "access_token" not in response.json()  # solo en cookie httponly
    assert "access_token" in response.cookies


def test_login_with_wrong_password_returns_401(client):
    client.post("/api/v1/auth/register", json=_register_payload())
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "mariachi.test@example.com", "password": "incorrecta"},
    )
    assert response.status_code == 401


def test_newly_registered_musician_can_get_profile_and_update_profile(client):
    reg = client.post("/api/v1/auth/register", json={
        "email": "nuevo.musico.profile@example.com",
        "password": "SuperSecreta123!",
        "role": "musician",
        "accepted_terms": True,
    })
    assert reg.status_code == 201

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "nuevo.musico.profile@example.com", "password": "SuperSecreta123!"},
    )
    assert login_resp.status_code == 200
    headers = {}  # sesión por cookie

    # Get profile (previously crashed with 500 ResponseValidationError)
    prof_resp = client.get("/api/v1/profiles/musician/me", headers=headers)
    assert prof_resp.status_code == 200
    prof_data = prof_resp.json()
    assert prof_data["stage_name"] is None
    assert prof_data["email"] == "nuevo.musico.profile@example.com"

    # Status endpoint
    status_resp = client.get("/api/v1/profiles/musician/me/status", headers=headers)
    assert status_resp.status_code == 200

    # Update profile in draft mode with username, fullname and stage_name
    update_resp = client.put(
        "/api/v1/profiles/musician",
        headers=headers,
        json={
            "username": "mariachi-nuevo-perfil",
            "fullname": "Musico Nuevo",
            "stage_name": "Mariachi El Sol",
        },
    )
    assert update_resp.status_code == 200
    updated_data = update_resp.json()
    assert updated_data["stage_name"] == "Mariachi El Sol"
    assert updated_data["username"] == "mariachi-nuevo-perfil"
