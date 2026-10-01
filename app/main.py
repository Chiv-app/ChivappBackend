from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import deps
from app.api.v1.api import api_router
from app.api.v1.endpoints import private_files
from app.core.config import settings
from app.core.limiter import limiter, rate_limit_exceeded_handler
from app.core.security import SecurityHeadersMiddleware
from app.db.migrate import run_migrations
from app.db.session import Base, engine
from app.services.uploads import ensure_upload_dir
import app.models  # noqa: F401 — registra modelos antes de create_all
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

DEV_CORS_ORIGIN_REGEX = (
    r"https?://(localhost|127\.0\.0\.1|.*\.ngrok-free\.app|.*\.ngrok\.io|.*\.loca\.lt)(:\d+)?"
)

run_migrations(engine)

IS_PRODUCTION = settings.ENVIRONMENT.lower() == "production"

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    # En producción no se publica el mapa completo de la API.
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)
# Solo para el esquema (x-forwarded-proto). La IP del cliente para rate limit y
# auditoría se resuelve en app.core.limiter.get_client_ip.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
# Aplica RATE_LIMIT_DEFAULT a todos los endpoints sin límite propio.
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    # En producción solo la lista explícita; en desarrollo también túneles y localhost.
    allow_origin_regex=None if IS_PRODUCTION else DEV_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def harden_upload_responses(request, call_next):
    """Los archivos subidos nunca se interpretan como HTML/JS en nuestro origen."""
    response = await call_next(request)
    if request.url.path.startswith("/uploads/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
        )
    return response


# Debe registrarse antes del mount estático de /uploads.
app.include_router(private_files.router)

ensure_upload_dir()
app.mount("/uploads", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "uploads")), name="uploads")

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
def health(db: Session = Depends(deps.get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
