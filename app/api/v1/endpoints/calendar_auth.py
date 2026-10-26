from urllib.parse import urlencode
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import settings
from app.core.jwt import create_signed_state, decode_signed_state
from app.models.user import User
from app.services.calendar_service import decrypt_refresh_token, encrypt_refresh_token

router = APIRouter()


@router.get("/url")
def get_auth_url(current_user: User = Depends(deps.get_current_user)):
    """Returns the Google OAuth login URL."""
    if not settings.GOOGLE_CLIENT_ID or not getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", None):
        raise HTTPException(500, "Google OAuth no configurado en servidor.")

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "access_type": "offline",
        "prompt": "consent",
        # Ligado al usuario: un callback con el state de otra cuenta se rechaza.
        "state": create_signed_state("calendar_state", {"uid": str(current_user.id)}),
    }

    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return {"auth_url": url}


@router.post("/callback")
def handle_callback(
    code: str = Query(...),
    state: str = Query(...),
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Exchanges the authorization code for a refresh token."""
    payload = decode_signed_state(state, "calendar_state")
    if not payload or payload.get("uid") != str(current_user.id):
        raise HTTPException(400, "La solicitud de conexión no es válida o expiró. Vuelve a intentarlo.")
    if (
        not settings.GOOGLE_CLIENT_ID
        or not settings.GOOGLE_CLIENT_SECRET
        or not getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", None)
    ):
        raise HTTPException(500, "Google OAuth no configurado.")

    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
    }

    try:
        response = httpx.post("https://oauth2.googleapis.com/token", data=data, timeout=10.0)
        response.raise_for_status()
        token_data = response.json()
    except httpx.HTTPError as e:
        logging.getLogger(__name__).error("Error exchange google token: %s", e)
        raise HTTPException(400, "No se pudo intercambiar el codigo con Google.")

    refresh_token = token_data.get("refresh_token")
    if refresh_token:
        current_user.google_calendar_refresh_token = encrypt_refresh_token(refresh_token)
        db.commit()
        return {"success": True, "message": "Calendario conectado exitosamente."}
    if "access_token" in token_data and current_user.google_calendar_refresh_token:
        return {"success": True, "message": "Calendario ya estaba conectado."}

    raise HTTPException(
        400,
        "Google no retorno un refresh token. Intenta desconectar la app en tu cuenta de Google y vuelve a intentar.",
    )


@router.delete("/disconnect")
def disconnect_calendar(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    raw_token = decrypt_refresh_token(current_user.google_calendar_refresh_token)
    current_user.google_calendar_refresh_token = None
    db.commit()
    if raw_token:
        try:
            httpx.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": raw_token},
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            logging.getLogger(__name__).warning(
                "No se pudo revocar el token de Google Calendar en Google: %s",
                exc,
            )
    return {"success": True, "message": "Calendario desconectado."}
