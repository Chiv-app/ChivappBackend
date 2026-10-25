import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Ensure backend root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.db.session import Base
import app.models  # noqa: F401 - Register all models with Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set database URL dynamically from app settings
config.set_main_option("sqlalchemy.url", settings.SQLALCHEMY_DATABASE_URI)

target_metadata = Base.metadata

_MANAGED_CUSTOM_INDEXES = {
    "uq_payment_gateway_payment_id",
    "uq_booking_review_final",
    "uq_contractor_document_number_normalized",
    "uq_musician_profile_slug_lower",
    "uq_musician_profile_stage_name_lower",
    "uq_payout_request_musician_pending",
    "uq_user_email_verification_token",
    "uq_user_password_reset_token",
    "uq_user_phone_normalized",
    "uq_user_username_lower",
    "ix_booking_complaint_status",
    "ix_support_ticket_status",
    "ix_payout_request_musician_id",
    "uq_musician_profile_slug",
}

_PRESERVED_LEGACY_COLUMNS = {
    "advance_amount",
    "pending_advance_amount",
}


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table" and name == "alembic_version":
        return False
    if type_ == "column" and reflected and compare_to is None and name in _PRESERVED_LEGACY_COLUMNS:
        return False
    if type_ == "index" and name in _MANAGED_CUSTOM_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = settings.SQLALCHEMY_DATABASE_URI

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        schema = os.environ.get("ALEMBIC_SCHEMA")
        configure_kwargs = {
            "connection": connection,
            "target_metadata": target_metadata,
            "compare_type": True,
            "include_object": include_object,
        }
        if schema:
            configure_kwargs["version_table_schema"] = schema

        context.configure(**configure_kwargs)

        with context.begin_transaction():
            from sqlalchemy import inspect as sa_inspect, text

            if schema:
                connection.execute(text(f"SET search_path TO {schema}"))

            inspector = sa_inspect(connection)
            existing_tables = set(inspector.get_table_names(schema=schema))
            if "user" in existing_tables:
                has_version = False
                if "alembic_version" in existing_tables:
                    row = connection.execute(
                        text("SELECT version_num FROM alembic_version LIMIT 1")
                    ).fetchone()
                    has_version = row is not None
                if not has_version:
                    connection.execute(
                        text(
                            "CREATE TABLE IF NOT EXISTS alembic_version ("
                            "version_num VARCHAR(32) NOT NULL, "
                            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                        )
                    )
                    connection.execute(
                        text("INSERT INTO alembic_version (version_num) VALUES ('c7a3bc7f5426')")
                    )

            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
