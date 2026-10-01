from datetime import datetime, timedelta

from jose import JWTError, jwt

from app.core.config import settings

SESSION_TYP = "access"


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, role: str, version: int = 0) -> str:
    """Token de sesión. `version` debe ser `user.token_version`: al incrementarla
    (cambio de contraseña, etc.) se invalidan todas las sesiones anteriores."""
    expire = datetime.utcnow() + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return _encode({"sub": subject, "role": role, "ver": version, "typ": SESSION_TYP, "exp": expire})


def create_magic_token(subject: str, redirect_to: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=24)
    return _encode({"sub": subject, "magic": True, "redirect_to": redirect_to, "exp": expire})


def create_oauth_pending_token(payload: dict, *, minutes: int = 30) -> str:
    expire = datetime.utcnow() + timedelta(minutes=minutes)
    return _encode({**payload, "typ": "oauth_pending", "exp": expire})


def create_signed_state(typ: str, payload: dict, *, minutes: int = 10) -> str:
    """Estado firmado y de corta duración para flujos OAuth (no es un token de sesión)."""
    expire = datetime.utcnow() + timedelta(minutes=minutes)
    return _encode({**payload, "typ": typ, "exp": expire})


def decode_signed_state(token: str | None, typ: str) -> dict | None:
    payload = decode_access_token(token) if token else None
    if not payload or payload.get("typ") != typ:
        return None
    return payload


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        return None


def decode_session_token(token: str) -> dict | None:
    """Decodifica un token de sesión. Rechaza magic links, estados OAuth y
    tokens pendientes, que comparten secreto pero no deben servir como cookie.
    Los tokens emitidos antes de agregar `typ` (sin ese campo) se aceptan."""
    payload = decode_access_token(token)
    if not payload or payload.get("magic"):
        return None
    if payload.get("typ") not in (None, SESSION_TYP):
        return None
    return payload


def session_version(payload: dict) -> int:
    try:
        return int(payload.get("ver") or 0)
    except (TypeError, ValueError):
        return -1
