from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware que añade cabeceras HTTP de seguridad a todas las respuestas del backend:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY (anti-clickjacking)
    - X-XSS-Protection: 1; mode=block
    - Referrer-Policy: strict-origin-when-cross-origin
    - Permissions-Policy: camera=(), microphone=(self), geolocation=(self)
    - Strict-Transport-Security (HSTS) en HTTPS o si está habilitado en configuración.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(self)"

        if request.url.scheme == "https" or getattr(settings, "ENABLE_HSTS", False):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response
