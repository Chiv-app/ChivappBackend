from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.core.config import settings
from app.models.oauth_account import OAuthProvider


@dataclass
class OAuthProfile:
    provider: OAuthProvider
    provider_user_id: str
    email: str | None
    fullname: str
    picture_url: str | None = None
    # Solo se vincula una cuenta existente por email si el proveedor confirma
    # que el email pertenece a quien inicia sesión.
    email_verified: bool = False


def _provider_or_400(provider: str) -> OAuthProvider:
    try:
        return OAuthProvider(provider)
    except ValueError as exc:
        raise HTTPException(400, "Proveedor OAuth no soportado") from exc


def oauth_configured(provider: OAuthProvider) -> bool:
    if provider == OAuthProvider.google:
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
    return bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET)


def redirect_uri(provider: OAuthProvider) -> str:
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return f"{base}/auth/oauth/{provider.value}/callback"


def build_authorize_url(provider: str, *, intent: str, state: str) -> str:
    p = _provider_or_400(provider)
    if not oauth_configured(p):
        raise HTTPException(
            503,
            f"OAuth con {p.value} no está configurado en el servidor.",
        )

    if p == OAuthProvider.google:
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri(p),
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "include_granted_scopes": "true",
            "prompt": "select_account",
            "state": state,
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    params = {
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": redirect_uri(p),
        "state": state,
        "scope": "email,public_profile",
        "response_type": "code",
    }
    return f"https://www.facebook.com/v19.0/dialog/oauth?{urlencode(params)}"


async def exchange_code_for_profile(provider: str, code: str) -> OAuthProfile:
    p = _provider_or_400(provider)
    if p == OAuthProvider.google:
        return await _google_profile(code)
    return await _facebook_profile(code)


async def _google_profile(code: str) -> OAuthProfile:
    token_url = "https://oauth2.googleapis.com/token"
    async with httpx.AsyncClient(timeout=20) as client:
        token_res = await client.post(
            token_url,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri(OAuthProvider.google),
                "grant_type": "authorization_code",
            },
        )
        if token_res.status_code >= 400:
            raise HTTPException(400, "No se pudo completar el login con Google")
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise HTTPException(400, "Google no devolvió access_token")

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_res.status_code >= 400:
            raise HTTPException(400, "No se pudo obtener el perfil de Google")
        data = user_res.json()

    sub = data.get("sub")
    if not sub:
        raise HTTPException(400, "Perfil de Google incompleto")
    email = data.get("email")
    name = data.get("name") or data.get("given_name") or (email.split("@")[0] if email else "Usuario")
    return OAuthProfile(
        provider=OAuthProvider.google,
        provider_user_id=str(sub),
        email=email.lower().strip() if email else None,
        fullname=name,
        picture_url=data.get("picture"),
        email_verified=data.get("email_verified") is True,
    )


async def _facebook_profile(code: str) -> OAuthProfile:
    async with httpx.AsyncClient(timeout=20) as client:
        token_res = await client.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "client_id": settings.FACEBOOK_APP_ID,
                "client_secret": settings.FACEBOOK_APP_SECRET,
                "redirect_uri": redirect_uri(OAuthProvider.facebook),
                "code": code,
            },
        )
        if token_res.status_code >= 400:
            raise HTTPException(400, "No se pudo completar el login con Facebook")
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise HTTPException(400, "Facebook no devolvió access_token")

        user_res = await client.get(
            "https://graph.facebook.com/me",
            params={
                "fields": "id,name,email,picture.type(large)",
                "access_token": access_token,
            },
        )
        if user_res.status_code >= 400:
            raise HTTPException(400, "No se pudo obtener el perfil de Facebook")
        data = user_res.json()

    fb_id = data.get("id")
    if not fb_id:
        raise HTTPException(400, "Perfil de Facebook incompleto")
    email = data.get("email")
    name = data.get("name") or (email.split("@")[0] if email else "Usuario")
    picture = None
    picture_data = data.get("picture") or {}
    if isinstance(picture_data, dict):
        picture = (picture_data.get("data") or {}).get("url")

    return OAuthProfile(
        provider=OAuthProvider.facebook,
        provider_user_id=str(fb_id),
        email=email.lower().strip() if email else None,
        fullname=name,
        picture_url=picture,
    )


async def verify_google_id_token(id_token: str) -> OAuthProfile:
    """Verifica un ID Token de Google (emitido por Google Identity Services / One Tap / Credential)."""
    token_url = "https://oauth2.googleapis.com/tokeninfo"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            res = await client.get(token_url, params={"id_token": id_token})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Error conectando con Google: {exc}") from exc

        if res.status_code >= 400:
            raise HTTPException(400, "Token de Google inválido o expirado")
        data = res.json()

    aud = data.get("aud")
    if settings.GOOGLE_CLIENT_ID and aud != settings.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "El token de Google no corresponde a esta aplicación")

    sub = data.get("sub")
    if not sub:
        raise HTTPException(400, "Perfil de Google incompleto")
    email = data.get("email")
    name = data.get("name") or data.get("given_name") or (email.split("@")[0] if email else "Usuario")
    return OAuthProfile(
        provider=OAuthProvider.google,
        provider_user_id=str(sub),
        email=email.lower().strip() if email else None,
        fullname=name,
        picture_url=data.get("picture"),
        email_verified=str(data.get("email_verified")).lower() == "true",
    )
