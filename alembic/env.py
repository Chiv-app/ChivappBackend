import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Ensure backend root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.db.alembic_bootstrap import stamp_legacy_connection
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

    from app.db.alembic_bootstrap import _validate_schema_name

    schema = _validate_schema_name(os.environ.get("ALEMBIC_SCHEMA"))
    connect_args = {"options": f"-csearch_path={schema}"} if schema else {}

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
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
            stamp_legacy_connection(connection, schema=schema)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
