import pytest


def _register_and_login(client, email="change.pwd@example.com", password="Password123!"):
    client.post("/api/v1/auth/register", json={
        "email": email,
        "password": password,
        "role": "musician",
        "accepted_terms": True,
    })
    login_res = client.post("/api/v1/auth/login", json={
        "email": email,
        "password": password,
    })
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_change_password_requires_auth(client):
    res = client.post("/api/v1/auth/change-password", json={
        "current_password": "OldPassword1!",
        "new_password": "NewPassword2@",
    })
    assert res.status_code == 401


def test_change_password_wrong_current_password_returns_400(client):
    headers = _register_and_login(client, email="user1@example.com", password="InitialPass1!")
    res = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": "WrongPassword1!",
            "new_password": "NewValidPass2@",
        },
    )
    assert res.status_code == 400
    assert "actual es incorrecta" in res.json()["detail"].lower()


def test_change_password_same_as_current_returns_400(client):
    headers = _register_and_login(client, email="user2@example.com", password="InitialPass1!")
    res = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": "InitialPass1!",
            "new_password": "InitialPass1!",
        },
    )
    assert res.status_code == 400
    assert "diferente a la actual" in res.json()["detail"].lower()


def test_change_password_weak_new_password_returns_400(client):
    headers = _register_and_login(client, email="user3@example.com", password="InitialPass1!")
    res = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": "InitialPass1!",
            "new_password": "WeakPassword1",
        },
    )
    assert res.status_code == 400
    assert "carácter especial" in res.json()["detail"].lower()


def test_change_password_successful(client):
    headers = _register_and_login(client, email="user4@example.com", password="InitialPass1!")
    res = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": "InitialPass1!",
            "new_password": "BrandNewPass2#",
        },
    )
    assert res.status_code == 200
    assert "exitosamente" in res.json()["message"].lower()

    login_old = client.post("/api/v1/auth/login", json={
        "email": "user4@example.com",
        "password": "InitialPass1!",
    })
    assert login_old.status_code == 401

    login_new = client.post("/api/v1/auth/login", json={
        "email": "user4@example.com",
        "password": "BrandNewPass2#",
    })
    assert login_new.status_code == 200
    assert login_new.json()["access_token"]
