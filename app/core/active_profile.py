"""Perfil artístico activo de la petición.

Un músico puede tener varios perfiles. La app elige con cuál trabaja mandando su id en la cabecera
`X-Musician-Profile-Id`. El valor solo sirve para escoger entre los perfiles de quien hace la petición:
quien lo usa debe comprobar que el perfil le pertenece (ver `app/services/musician_profiles.py`).
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

ACTIVE_PROFILE_HEADER = b"x-musician-profile-id"

# None = sin elección: se usa el perfil principal.
active_profile_id: ContextVar[UUID | None] = ContextVar("active_profile_id", default=None)


def parse_profile_id(raw: bytes | str | None) -> UUID | None:
    if not raw:
        return None
    try:
        text = raw.decode("ascii") if isinstance(raw, bytes) else raw
        return UUID(text.strip())
    except (ValueError, UnicodeDecodeError):
        return None


class ActiveProfileMiddleware:
    """ASGI puro: guarda el perfil elegido en el contexto de la petición, sin tocar la respuesta."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((value for key, value in scope.get("headers", []) if key == ACTIVE_PROFILE_HEADER), None)
        token = active_profile_id.set(parse_profile_id(raw))
        try:
            await self.app(scope, receive, send)
        finally:
            active_profile_id.reset(token)
