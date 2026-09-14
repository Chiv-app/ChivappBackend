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
from app.core.config import settings
from app.core.limiter import limiter, rate_limit_exceeded_handler
from app.core.security import SecurityHeadersMiddleware
from app.db.migrate import run_migrations
from app.db.session import Base, engine
from app.services.uploads import ensure_upload_dir
import app.models  # noqa: F401 — registra modelos antes de create_all
from slowapi.errors import RateLimitExceeded

run_migrations(engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|chiv\.app|.*\.chiv\.app|.*\.ngrok-free\.app|.*\.ngrok\.io|.*\.loca\.lt)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

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
