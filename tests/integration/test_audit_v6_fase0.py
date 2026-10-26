"""Tests de regresión para la Fase 0 del Plan de Cierre (Auditoría v6).

Cubre:
- SEC6-01: BACKEND_CORS_ORIGINS con NoDecode (acepta JSON y lista separada por comas desde env)
- BUG6-01: Migración de esquema legado sin tablas booking_link / payout_request / uploaded_file
- BUG6-02: Detección explícita de duplicados antes de CREATE UNIQUE INDEX en d8e9f0a1b2c3
- SEC6-06 / BUG6-12: Tres escenarios de Alembic (vacía, create_all, estampada) + candado advisory + sin duplicación en env.py
- SEC6-05 / BUG6-06: Timeouts cortos de Redis (0.5 s) + fallback en memoria en < 3 s
- SEC6-07: Scripts deploy.sh y deploy.ps1 con Secret Manager, validación previa y montaje unificado
- SEC6-08: x-chivapp-proxy-secret no ASCII no lanza TypeError (500)
- SEC6-10 / BUG6-05: Workflows de CI unificados con Postgres, ruff, pip-audit, alembic check y pytest completo
- SEC3-07 / BUG6-08: ENVIRONMENT="production" por defecto + validaciones de 127.0.0.1, ENABLE_HSTS, CORS vacío y GOOGLE_OAUTH_REDIRECT_URI
- BUG6-09: downgrade() destructivos bloqueados con RuntimeError claro
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.core.limiter import REDIS_STORAGE_OPTIONS, build_limiter, get_client_ip
from app.db.alembic_bootstrap import (
    ALEMBIC_ADVISORY_LOCK_ID,
    BASELINE_REVISION,
    ensure_alembic_stamped,
)
from app.db.session import Base
import app.models  # noqa: F401


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


def _valid_prod_env() -> dict[str, str]:
    return {
        "ENVIRONMENT": "production",
        "ENABLE_HSTS": "True",
        "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg2://prod_user:StrongProdPass123!@10.0.0.5:5432/chivapp_prod",
        "JWT_SECRET_KEY": "prod-jwt-secret-key-with-more-than-32-chars-ok",
        "PROXY_SHARED_SECRET": "prod-proxy-shared-secret-with-32-plus-chars-ok",
        "GCP_BUCKET_NAME": "chivapp-uploads",
        "GCP_PRIVATE_BUCKET_NAME": "chivapp-private-uploads",
        "RATE_LIMIT_STORAGE_URI": "redis://10.0.0.6:6379/0",
        "FRONTEND_URL": "https://chiv.app",
        "BACKEND_CORS_ORIGINS": '["https://chiv.app","https://www.chiv.app","https://api.chiv.app"]',
        "MERCADO_PAGO_SANDBOX": "False",
    }


def test_sec6_01_cors_origins_env_accepts_csv_and_json(monkeypatch: pytest.MonkeyPatch):
    """SEC6-01: Settings(_env_file=None) acepta BACKEND_CORS_ORIGINS en comas y en JSON."""
    for k, v in _valid_prod_env().items():
        monkeypatch.setenv(k, v)

    # Caso 1: Cadena separada por comas en variable de entorno (antes fallaba con JSONDecodeError en pydantic-settings)
    monkeypatch.setenv(
        "BACKEND_CORS_ORIGINS",
        "https://chiv.app,https://www.chiv.app,https://api.chiv.app",
    )
    cfg_csv = Settings(_env_file=None)
    assert cfg_csv.BACKEND_CORS_ORIGINS == [
        "https://chiv.app",
        "https://www.chiv.app",
        "https://api.chiv.app",
    ]

    # Caso 2: JSON array en variable de entorno
    monkeypatch.setenv(
        "BACKEND_CORS_ORIGINS",
        '["https://chiv.app","https://www.chiv.app","https://api.chiv.app"]',
    )
    cfg_json = Settings(_env_file=None)
    assert cfg_json.BACKEND_CORS_ORIGINS == [
        "https://chiv.app",
        "https://www.chiv.app",
        "https://api.chiv.app",
    ]


def test_sec3_07_bug6_08_production_default_and_strict_validators(
    monkeypatch: pytest.MonkeyPatch,
):
    """SEC3-07 / BUG6-08: ENVIRONMENT por defecto es 'production' y valida 127.0.0.1, HSTS, CORS y OAuth."""
    for k, v in _valid_prod_env().items():
        monkeypatch.setenv(k, v)

    # 1. Sin ENVIRONMENT en el entorno, por defecto es "production"
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    cfg_default = Settings(_env_file=None)
    assert cfg_default.ENVIRONMENT == "production"
    assert cfg_default.is_production is True

    # 2. Credenciales locales por defecto en 127.0.0.1 rechazadas
    with pytest.raises(ValueError, match="SQLALCHEMY_DATABASE_URI"):
        Settings(
            _env_file=None,
            SQLALCHEMY_DATABASE_URI="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/musicdb",
        )

    # 3. ENABLE_HSTS=False rechazado en producción
    with pytest.raises(ValueError, match="ENABLE_HSTS"):
        Settings(_env_file=None, ENABLE_HSTS=False)

    # 4. BACKEND_CORS_ORIGINS vacío tras filtrar localhost rechazado
    with pytest.raises(ValueError, match="BACKEND_CORS_ORIGINS"):
        Settings(
            _env_file=None,
            BACKEND_CORS_ORIGINS=["http://localhost:3000", "http://127.0.0.1:3000"],
        )

    # 5. GOOGLE_CLIENT_ID activo con GOOGLE_OAUTH_REDIRECT_URI apuntando a localhost rechazado
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_REDIRECT_URI"):
        Settings(
            _env_file=None,
            GOOGLE_CLIENT_ID="google-client-id-prod",
            GOOGLE_OAUTH_REDIRECT_URI="http://localhost:3000/calendar-callback",
        )


def test_sec6_08_non_ascii_proxy_secret_header_does_not_raise_500(
    monkeypatch: pytest.MonkeyPatch,
):
    """SEC6-08: Cabecera x-chivapp-proxy-secret con caracteres no ASCII no lanza TypeError."""
    monkeypatch.setattr(
        settings, "PROXY_SHARED_SECRET", "secreto-compartido-ascii-de-mas-de-32-bytes"
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/health",
        "headers": [
            (b"x-chivapp-proxy-secret", "ñ-secreto-no-ascii-€".encode("utf-8")),
            (b"x-chivapp-client-ip", b"198.51.100.77"),
            (b"x-forwarded-for", b"10.0.0.1, 203.0.113.42"),
        ],
        "client": ("127.0.0.1", 54321),
    }
    request = Request(scope)
    assert get_client_ip(request) == "203.0.113.42"


def test_sec6_05_bug6_06_unreachable_redis_responds_quickly():
    """SEC6-05 / BUG6-06: Limitador con Redis inalcanzable tiene timeout 0.5 s y responde en < 3 s."""
    assert REDIS_STORAGE_OPTIONS.get("socket_connect_timeout") == 0.5
    assert REDIS_STORAGE_OPTIONS.get("socket_timeout") == 0.5

    test_limiter = build_limiter(storage_uri="redis://127.0.0.1:1", enabled=True)
    assert test_limiter._in_memory_fallback_enabled is True
    assert test_limiter._storage_options.get("socket_connect_timeout") == 0.5
    assert test_limiter._storage_options.get("socket_timeout") == 0.5

    mini_app = FastAPI()
    mini_app.state.limiter = test_limiter

    @mini_app.get("/limited-ping")
    @test_limiter.limit("5/minute")
    def limited_ping(request: Request):
        return {"ok": True}

    client = TestClient(mini_app)
    t0 = time.monotonic()
    resp = client.get("/limited-ping")
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 3.0, f"La petición con Redis inalcanzable tardó {elapsed:.2f}s (>= 3.0s)"


def test_bug6_09_all_migrations_block_destructive_downgrade():
    """BUG6-09: Todas las migraciones lanzan RuntimeError claro al invocar downgrade()."""
    versions_dir = BACKEND_DIR / "alembic" / "versions"
    migration_files = sorted(versions_dir.glob("*.py"))
    assert len(migration_files) >= 10

    for mig_path in migration_files:
        spec = importlib.util.spec_from_file_location(mig_path.stem, mig_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with pytest.raises(RuntimeError, match="Downgrade no soportado"):
            mod.downgrade()


def test_sec6_06_bug6_12_no_duplicate_stamping_in_alembic_env():
    """SEC6-06 / BUG6-12: alembic/env.py delega en stamp_legacy_connection y no duplica SQL de estampado."""
    env_source = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "stamp_legacy_connection" in env_source
    assert "INSERT INTO alembic_version" not in env_source
    assert ALEMBIC_ADVISORY_LOCK_ID == 72839401


def _run_alembic_in_schema(db_url: str, schema_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ENVIRONMENT"] = "development"
    env["SQLALCHEMY_DATABASE_URI"] = db_url
    env["ALEMBIC_SCHEMA"] = schema_name
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sec6_06_bug6_01_three_database_migration_scenarios():
    """SEC6-06 / BUG6-01 / BUG6-12: Tres escenarios de base (vacía, create_all legado sin tablas nuevas, estampada)."""
    db_url = settings.SQLALCHEMY_DATABASE_URI
    engine = sa.create_engine(db_url, poolclass=sa.pool.NullPool)
    schema_empty = f"test_mig_empty_{uuid.uuid4().hex[:8]}"
    schema_legacy = f"test_mig_legacy_{uuid.uuid4().hex[:8]}"

    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_empty}"'))
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_legacy}"'))

        # Escenario 1: Base completamente vacía -> upgrade head + check
        res_up = _run_alembic_in_schema(db_url, schema_empty, "upgrade", "head")
        assert res_up.returncode == 0, f"Fallo upgrade head en base vacía:\n{res_up.stderr}"
        res_chk = _run_alembic_in_schema(db_url, schema_empty, "check")
        assert res_chk.returncode == 0, f"Fallo alembic check en base vacía:\n{res_chk.stderr}"

        # Escenario 2: Base legada creada con create_all PERO sin booking_link, payout_request ni uploaded_file (BUG6-01)
        legacy_tables = [
            t
            for name, t in Base.metadata.tables.items()
            if name not in {"booking_link", "payout_request", "payout_request_booking", "uploaded_file"}
        ]
        with engine.begin() as conn:
            conn.execute(sa.text(f'SET search_path TO "{schema_legacy}"'))
            Base.metadata.create_all(bind=conn, tables=legacy_tables)

        stamped = ensure_alembic_stamped(database_uri=db_url, schema=schema_legacy)
        assert stamped is True

        # Escenario 3: Base ya estampada -> ensure_alembic_stamped es idempotente y devuelve False
        stamped_again = ensure_alembic_stamped(database_uri=db_url, schema=schema_legacy)
        assert stamped_again is False

        res_legacy_up = _run_alembic_in_schema(db_url, schema_legacy, "upgrade", "head")
        assert res_legacy_up.returncode == 0, (
            f"Fallo upgrade head en base legada sin booking_link/payout_request:\n{res_legacy_up.stderr}"
        )

        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{schema_legacy}"'))
            for required_table in (
                "booking_link",
                "payout_request",
                "payout_request_booking",
                "uploaded_file",
                "alembic_version",
            ):
                reg = conn.execute(
                    sa.text("SELECT to_regclass(:t)"), {"t": required_table}
                ).scalar()
                assert reg == required_table
            rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "d8e9f0a1b2c3"
            assert BASELINE_REVISION == "c7a3bc7f5426"
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema_empty}" CASCADE'))
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema_legacy}" CASCADE'))
        engine.dispose()


def test_bug6_02_duplicate_phone_fails_with_clear_error_message():
    """BUG6-02: Dos usuarios con el mismo teléfono antes de d8e9f0a1b2c3 producen un error explícito con instrucciones."""
    db_url = settings.SQLALCHEMY_DATABASE_URI
    engine = sa.create_engine(db_url, poolclass=sa.pool.NullPool)
    schema_dup = f"test_mig_dup_{uuid.uuid4().hex[:8]}"

    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_dup}"'))

        # Migrar hasta c7a3bc7f5426 (antes de d8e9f0a1b2c3)
        res_pre = _run_alembic_in_schema(db_url, schema_dup, "upgrade", "c7a3bc7f5426")
        assert res_pre.returncode == 0, f"Fallo upgrade a c7a3bc7f5426:\n{res_pre.stderr}"

        # Insertar dos usuarios con el mismo teléfono
        with engine.begin() as conn:
            conn.execute(sa.text(f'SET search_path TO "{schema_dup}"'))
            conn.execute(
                sa.text(
                    'INSERT INTO "user" (id, email, password_hash, fullname, phone, role, is_active) '
                    "VALUES (:id1, 'dup1@example.com', 'h', 'Dup 1', '+51987654321', 'contractor', true), "
                    "(:id2, 'dup2@example.com', 'h', 'Dup 2', '+51987654321', 'contractor', true)"
                ),
                {"id1": str(uuid.uuid4()), "id2": str(uuid.uuid4())},
            )

        # Intentar aplicar d8e9f0a1b2c3 debe fallar con mensaje explícito nombrando el índice y el teléfono duplicado
        res_dup = _run_alembic_in_schema(db_url, schema_dup, "upgrade", "head")
        assert res_dup.returncode != 0
        combined_out = (res_dup.stdout or "") + "\n" + (res_dup.stderr or "")
        assert "uq_user_phone_normalized" in combined_out
        assert "+51987654321" in combined_out
        assert "HAVING COUNT(*) > 1" in combined_out
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema_dup}" CASCADE'))
        engine.dispose()


def test_sec6_07_sec6_10_deploy_scripts_and_ci_workflows_hardened():
    """SEC6-07 / SEC6-10 / BUG6-05: Scripts de despliegue y workflows CI están endurecidos y sincronizados."""
    deploy_sh = (REPO_ROOT / "infra" / "gcp" / "deploy.sh").read_text(encoding="utf-8")
    deploy_ps1 = (REPO_ROOT / "infra" / "gcp" / "deploy.ps1").read_text(encoding="utf-8")

    for script_name, content in [("deploy.sh", deploy_sh), ("deploy.ps1", deploy_ps1)]:
        assert '["https://chiv.app","https://www.chiv.app","https://api.chiv.app"]' in content, script_name
        assert "--update-secrets=" in content, script_name
        assert "GOOGLE_OAUTH_REDIRECT_URI" in content, script_name
        assert "mount-path=/app/uploads" in content, script_name
        assert "Settings(_env_file=None)" in content, script_name

    root_ci = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    backend_ci = (BACKEND_DIR / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8")

    for wf_name, wf in [("tests.yml", root_ci), ("ci-cd.yml", backend_ci)]:
        assert "postgres:16-alpine" in wf, wf_name
        assert "pip-audit -r requirements.txt" in wf, wf_name
        assert "alembic upgrade head" in wf, wf_name
        assert "alembic check" in wf, wf_name
        assert "Settings(_env_file=None)" in wf, wf_name
        assert "--ignore=tests/integration" not in wf, wf_name
