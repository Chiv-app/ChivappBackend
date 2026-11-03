from app.models.user import User
from app.services.email.defaults import APP_NAME


def build_sample_email_context(user: User) -> dict[str, str]:
    return {
        "user_name": user.fullname,
        "user_email": user.email,
        "musician_name": user.fullname,
        "contractor_name": "Carlos Mendoza (Cliente)",
        "member_name": "María Integrante",
        "member_email": "integrante@email.com",
        "leader_name": user.fullname,
        "app_name": APP_NAME,
        "login_url": "https://chiv.app/",
        "action_url": "https://chiv.app/musician/bookings/preview-demo",
        "expires_hours": "48",
        "expires_days": "14",
        "specialties": "Guitarra, Voz",
        "event_type": "Boda",
        "event_date": "15/08/2026",
        "event_time": "19:00",
        "event_location": "Salón Jardín, Miraflores, Lima",
        "event_description": "Requerimos presentación musical en vivo de 1 hora para recepción de boda. Repertorio variado y acústico.",
    }
