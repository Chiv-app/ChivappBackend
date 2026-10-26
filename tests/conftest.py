import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# --- Variables de entorno de test, ANTES de importar cualquier módulo de app.* ---
# app/main.py ejecuta create_all + run_migrations al importar el módulo, usando
# el engine construido desde SQLALCHEMY_DATABASE_URI en ese momento.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mariachi_test"
)
if not TEST_DATABASE_URL.split("?")[0].rstrip("/").endswith("_test"):
    raise RuntimeError(
        f"Refusing to run tests against non-test database: {TEST_DATABASE_URL}"
    )
os.environ["SQLALCHEMY_DATABASE_URI"] = TEST_DATABASE_URL
os.environ["ENVIRONMENT"] = "development"
os.environ["JWT_SECRET_KEY"] = "test-secret"
os.environ["RESEND_API_KEY"] = ""
os.environ["BREVO_API_KEY"] = ""
os.environ["EMAIL_ENABLED"] = "false"
os.environ["MERCADO_PAGO_ACCESS_TOKEN"] = ""
os.environ["MERCADO_PAGO_PUBLIC_KEY"] = ""
os.environ["GOOGLE_CREDENTIALS_JSON"] = ""
os.environ["GOOGLE_CALENDAR_ID"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GCP_BUCKET_NAME"] = ""
os.environ["GCP_PRIVATE_BUCKET_NAME"] = ""
os.environ["FRONTEND_URL"] = "http://localhost:3000"


def _db_reachable(url: str) -> bool:
    # No basta con ver si el puerto está abierto: puede haber otro Postgres local
    # (u otro servicio) escuchando ahí con credenciales/DB distintas. Se intenta
    # una conexión y query real; cualquier fallo se trata como "no disponible".
    try:
        probe_engine = create_engine(url, future=True)
        with probe_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe_engine.dispose()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_reachable(TEST_DATABASE_URL)

if DB_AVAILABLE:
    import app.main as main_module
    from app.api import deps
    from app.db.migrate import run_migrations
    from app.db.session import Base
    from app.main import app  # noqa: F401

    engine = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    main_module._startup_initialized = True
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def pytest_collection_modifyitems(config, items):
    if DB_AVAILABLE:
        return
    skip_db = pytest.mark.skip(
        reason="requiere Postgres local o TEST_DATABASE_URL (ver Backend/tests/conftest.py)"
    )
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


@pytest.fixture(autouse=True)
def _disable_external_calendar(monkeypatch):
    """Evita que cualquier test conecte al Google Calendar real aunque exista google_credentials.json en disco."""
    import app.services.calendar_service as cal_svc
    from app.core.config import settings

    monkeypatch.setattr(settings, "GOOGLE_CREDENTIALS_JSON", "")
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ID", "")
    monkeypatch.setattr(cal_svc, "_get_calendar_service", lambda: None)
    monkeypatch.setattr(cal_svc, "_get_calendar_service_oauth", lambda *_a, **_kw: None)
    monkeypatch.setattr(cal_svc, "create_booking_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(cal_svc, "cancel_booking_calendar_event", lambda *_a, **_kw: None)


@pytest.fixture
def db_session():
    if not DB_AVAILABLE:
        pytest.skip("requiere Postgres local o TEST_DATABASE_URL")

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        # El código de la app hace sus propios db.commit() dentro de los endpoints,
        # lo que rompe el patrón clásico de SAVEPOINT + rollback. Se limpia la DB
        # truncando todas las tablas al final de cada test — más simple y robusto.
        table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            with engine.begin() as conn:
                conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient
    from app.core.limiter import limiter

    limiter.reset()
    app.dependency_overrides[deps.get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        limiter.reset()
        app.dependency_overrides.pop(deps.get_db, None)
