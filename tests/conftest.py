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
os.environ["SQLALCHEMY_DATABASE_URI"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("RESEND_API_KEY", "")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")


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
    from app.api import deps
    from app.db.session import Base
    from app.main import app  # noqa: F401  (dispara create_all + run_migrations)

    engine = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(bind=engine)
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
