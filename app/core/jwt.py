from datetime import datetime, timedelta

from jose import JWTError, jwt

from app.core.config import settings


def create_access_token(subject: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode = {"sub": subject, "role": role, "exp": expire}
    return jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_magic_token(subject: str, redirect_to: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode = {"sub": subject, "magic": True, "redirect_to": redirect_to, "exp": expire}
    return jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_oauth_pending_token(payload: dict, *, minutes: int = 30) -> str:
    expire = datetime.utcnow() + timedelta(minutes=minutes)
    to_encode = {**payload, "typ": "oauth_pending", "exp": expire}
    return jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


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
    """Decodifica un token de sesión. Rechaza magic links y tokens OAuth
    pendientes, que comparten secreto pero no deben servir como cookie."""
    payload = decode_access_token(token)
    if not payload or payload.get("magic") or payload.get("typ"):
        return None
    return payload
