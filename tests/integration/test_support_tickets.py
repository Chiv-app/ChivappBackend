import pytest

from app.core.hashing import hash_password
from app.models.support_ticket import SupportTicket, SupportTicketStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _login(client, email, password):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _admin(db):
    admin = User(
        email="support-admin@test.com",
        fullname="Admin Test",
        role=UserRole.admin,
        password_hash=hash_password("AdminPass123"),
        is_verified=True,
    )
    db.add(admin)
    db.commit()
    return admin


def test_anonymous_ticket_requires_name_and_email(client):
    response = client.post(
        "/api/v1/support/tickets",
        json={"message": "Tengo un problema con mi reserva"},
    )
    assert response.status_code == 400


def test_anonymous_ticket_captures_metadata(client, db_session):
    response = client.post(
        "/api/v1/support/tickets",
        json={
            "message": "No puedo subir mi comprobante de pago",
            "guest_name": "Visitante Anónimo",
            "guest_email": "visitante@example.com",
            "page_path": "/musicians/mariachi-vargas",
        },
        headers={"user-agent": "pytest-agent/1.0"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "open"
    assert body["admin_response"] is None

    ticket = db_session.query(SupportTicket).filter(SupportTicket.id == body["id"]).first()
    assert ticket.guest_name == "Visitante Anónimo"
    assert ticket.guest_email == "visitante@example.com"
    assert ticket.user_id is None
    assert ticket.submitted_user_agent == "pytest-agent/1.0"
    assert ticket.submitted_path == "/musicians/mariachi-vargas"
    assert ticket.submitted_ip


def test_logged_in_user_ticket_and_history(client, db_session):
    user = User(
        email="musico-soporte@test.com",
        fullname="Músico Soporte",
        role=UserRole.musician,
        password_hash=hash_password("MusicPass123"),
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    _login(client, user.email, "MusicPass123")

    created = client.post(
        "/api/v1/support/tickets",
        json={"message": "¿Cómo actualizo mi portafolio?"},
    )
    assert created.status_code == 201, created.text

    ticket = db_session.query(SupportTicket).filter(SupportTicket.id == created.json()["id"]).first()
    assert ticket.user_id == user.id
    assert ticket.guest_name is None

    mine = client.get("/api/v1/support/tickets/me")
    assert mine.status_code == 200
    assert len(mine.json()) == 1


def test_admin_lists_and_responds_to_ticket(client, db_session):
    admin = _admin(db_session)
    ticket = SupportTicket(message="Necesito ayuda con un cobro", guest_name="Ana", guest_email="ana@example.com")
    db_session.add(ticket)
    db_session.commit()

    musician = User(
        email="musico-noadmin@test.com",
        fullname="No Admin",
        role=UserRole.musician,
        password_hash=hash_password("MusicPass123"),
        is_verified=True,
    )
    db_session.add(musician)
    db_session.commit()

    _login(client, musician.email, "MusicPass123")
    forbidden = client.get("/api/v1/admin/support/tickets")
    assert forbidden.status_code == 403

    _login(client, admin.email, "AdminPass123")

    listed = client.get("/api/v1/admin/support/tickets")
    assert listed.status_code == 200
    assert any(item["id"] == str(ticket.id) for item in listed.json())
    item = next(item for item in listed.json() if item["id"] == str(ticket.id))
    assert item["submitter_name"] == "Ana"
    assert item["submitter_email"] == "ana@example.com"

    responded = client.post(
        f"/api/v1/admin/support/tickets/{ticket.id}/respond",
        json={"response": "Ya revisamos tu cobro, todo en orden.", "status": "resolved"},
    )
    assert responded.status_code == 200, responded.text
    body = responded.json()
    assert body["status"] == "resolved"
    assert body["admin_response"] == "Ya revisamos tu cobro, todo en orden."

    db_session.refresh(ticket)
    assert ticket.status == SupportTicketStatus.resolved
    assert ticket.responded_by_admin_id == admin.id
    assert ticket.responded_at is not None
