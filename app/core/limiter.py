import hmac

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.config import settings


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
        if forwarded_ip and hmac.compare_digest(sent, secret):
            return forwarded_ip
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    enabled=settings.RATE_LIMITING_ENABLED,
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
)


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

