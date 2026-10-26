import hmac

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.config import settings

REDIS_STORAGE_OPTIONS = {
    "socket_connect_timeout": 0.5,
    "socket_timeout": 0.5,
}


def get_client_ip(request: Request) -> str:
    """IP real del cliente, sin confiar en cabeceras que él mismo puede enviar.

    1. Si la petición viene del proxy de Next.js (secreto compartido válido), se
       usa la IP que éste reenvía en x-chivapp-client-ip.
    2. Si no, la ÚLTIMA entrada de X-Forwarded-For: la agrega el balanceador de
       Cloud Run; las anteriores las puede inventar el cliente.
    """
    secret = settings.PROXY_SHARED_SECRET
    if secret:
        sent = request.headers.get("x-chivapp-proxy-secret") or ""
        forwarded_ip = (request.headers.get("x-chivapp-client-ip") or "").strip()
        if (
            forwarded_ip
            and sent
            and sent.isascii()
            and hmac.compare_digest(sent.encode("utf-8"), secret.encode("utf-8"))
        ):
            return forwarded_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def build_limiter(
    storage_uri: str | None = None,
    enabled: bool | None = None,
) -> Limiter:
    effective_uri = storage_uri or settings.RATE_LIMIT_STORAGE_URI
    return Limiter(
        key_func=get_client_ip,
        default_limits=[settings.RATE_LIMIT_DEFAULT],
        enabled=settings.RATE_LIMITING_ENABLED if enabled is None else enabled,
        storage_uri=effective_uri,
        storage_options=dict(REDIS_STORAGE_OPTIONS),
        in_memory_fallback_enabled=True,
    )


limiter = build_limiter()


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    headers = {"Retry-After": "60"}
    return JSONResponse(
        {
            "detail": "Demasiadas peticiones. Por favor, intenta de nuevo en un momento.",
            "error": f"Rate limit exceeded: {exc.detail}",
        },
        status_code=429,
        headers=headers,
    )
