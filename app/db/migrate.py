from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.profile_helpers import (
    backfill_musician_slugs,
    backfill_user_usernames,
    ensure_role_profiles,
    fix_unverified_published_profiles,
)
from app.db.session import SessionLocal
from app.services.email.service import ensure_email_templates


MIGRATION_STATEMENTS = [
    "ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'admin'",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'draft'",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS songs VARCHAR[] DEFAULT '{}'",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS repertoire JSONB DEFAULT '[]'::jsonb",
    """
    UPDATE musician_profile
    SET repertoire = (
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object('title', song, 'youtube_url', NULL)
            ),
            '[]'::jsonb
        )
        FROM unnest(COALESCE(songs, '{}'::varchar[])) AS song
    )
    WHERE (repertoire IS NULL OR repertoire = '[]'::jsonb)
      AND songs IS NOT NULL
      AND cardinality(songs) > 0
    """,
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS id_document_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS contract_template_title VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS contract_template_body TEXT",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS contract_pdf_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS instagram_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS facebook_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS tiktok_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS youtube_channel_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS spotify_url VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS website_url VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'draft'",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS published_at TIMESTAMP",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS id_document_url VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS document_type VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS document_number VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS address VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS city VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS contract_template_title VARCHAR",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS contract_template_body TEXT",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS contract_pdf_url VARCHAR",
    "ALTER TABLE booking ALTER COLUMN price_agreed DROP NOT NULL",
    "ALTER TABLE booking ALTER COLUMN end_time DROP NOT NULL",
    "ALTER TABLE booking ALTER COLUMN location_reference DROP NOT NULL",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS musician_quote_notes VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS quoted_at TIMESTAMP",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS requested_repertoire JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS terms_accepted BOOLEAN DEFAULT FALSE",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS terms_accepted_ip VARCHAR",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS contractor_signature_url VARCHAR",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS title VARCHAR",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS body TEXT",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS context JSONB",
    "ALTER TABLE contract ALTER COLUMN contract_pdf_url DROP NOT NULL",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS payment_type VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS evidence_url VARCHAR",
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'change_pending'",
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'balance_pending'",
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'balance_review'",
    "ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'in_progress'",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_location_address VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_location_city VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_location_reference VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_event_description VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_change_notes VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_price_agreed NUMERIC",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS pending_advance_amount NUMERIC",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS change_requested_by VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS change_requested_at TIMESTAMP",
    # Allow multiple event reviews / reactions per booking (live show timeline).
    "ALTER TABLE booking_review DROP CONSTRAINT IF EXISTS booking_review_booking_id_key",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS share_token VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS share_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS share_enabled_at TIMESTAMP",
    "ALTER TABLE booking_review ALTER COLUMN author_user_id DROP NOT NULL",
    "ALTER TABLE booking_review ADD COLUMN IF NOT EXISTS guest_name VARCHAR",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_share_token ON booking (share_token)",
    "ALTER TABLE \"user\" ALTER COLUMN password_hash DROP NOT NULL",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    "UPDATE \"user\" SET is_active = TRUE WHERE is_active IS NULL",
    "ALTER TABLE booking_review ADD COLUMN IF NOT EXISTS is_final BOOLEAN DEFAULT FALSE",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS rating_avg NUMERIC(2,1)",
    "ALTER TABLE contractor_profile ADD COLUMN IF NOT EXISTS rating_count INTEGER",
    """
    CREATE TABLE IF NOT EXISTS oauth_account (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES \"user\"(id) ON DELETE CASCADE,
        provider VARCHAR NOT NULL,
        provider_user_id VARCHAR NOT NULL,
        email VARCHAR,
        created_at TIMESTAMP,
        CONSTRAINT uq_oauth_provider_user UNIQUE (provider, provider_user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_oauth_account_user_id ON oauth_account (user_id)",
    """
    CREATE TABLE IF NOT EXISTS contractor_recommendation (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        musician_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        contractor_id UUID NOT NULL REFERENCES contractor_profile(id) ON DELETE CASCADE,
        rating INTEGER NOT NULL,
        comment TEXT NOT NULL,
        created_at TIMESTAMP,
        CONSTRAINT uq_contractor_recommendation_booking UNIQUE (booking_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_contractor_recommendation_musician_id ON contractor_recommendation (musician_id)",
    "CREATE INDEX IF NOT EXISTS ix_contractor_recommendation_contractor_id ON contractor_recommendation (contractor_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_review_final
    ON booking_review (booking_id)
    WHERE is_final IS TRUE
    """,
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS showreel_video_url VARCHAR",
    """
    DO $$ BEGIN
        CREATE TYPE ensemblememberstatus AS ENUM ('invited', 'active', 'inactive');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    DO $$ BEGIN
        CREATE TYPE bookingmemberinvitestatus AS ENUM ('pending', 'accepted', 'declined');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    DO $$ BEGIN
        CREATE TYPE bookingmemberpayoutstatus AS ENUM ('draft', 'locked', 'paid');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS ensemble_member (
        id UUID PRIMARY KEY,
        leader_user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        member_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        email VARCHAR NOT NULL,
        fullname VARCHAR NOT NULL,
        phone VARCHAR,
        specialties VARCHAR[] NOT NULL DEFAULT '{}',
        notes TEXT,
        status ensemblememberstatus NOT NULL DEFAULT 'invited',
        password_setup_token VARCHAR UNIQUE,
        password_setup_expires_at TIMESTAMP,
        invited_at TIMESTAMP,
        joined_at TIMESTAMP,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        CONSTRAINT uq_ensemble_member_leader_email UNIQUE (leader_user_id, email)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ensemble_member_leader_user_id ON ensemble_member (leader_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_ensemble_member_member_user_id ON ensemble_member (member_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_ensemble_member_email ON ensemble_member (email)",
    "CREATE INDEX IF NOT EXISTS ix_ensemble_member_password_setup_token ON ensemble_member (password_setup_token)",
    """
    CREATE TABLE IF NOT EXISTS booking_member_invite (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        ensemble_member_id UUID NOT NULL REFERENCES ensemble_member(id) ON DELETE CASCADE,
        status bookingmemberinvitestatus NOT NULL DEFAULT 'pending',
        response_token VARCHAR NOT NULL UNIQUE,
        invited_at TIMESTAMP,
        responded_at TIMESTAMP,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        CONSTRAINT uq_booking_member_invite UNIQUE (booking_id, ensemble_member_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_member_invite_booking_id ON booking_member_invite (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_member_invite_ensemble_member_id ON booking_member_invite (ensemble_member_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_member_invite_response_token ON booking_member_invite (response_token)",
    """
    CREATE TABLE IF NOT EXISTS booking_member_payout (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        ensemble_member_id UUID NOT NULL REFERENCES ensemble_member(id) ON DELETE CASCADE,
        amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
        currency VARCHAR NOT NULL DEFAULT 'PEN',
        status bookingmemberpayoutstatus NOT NULL DEFAULT 'draft',
        note TEXT,
        set_by_leader_at TIMESTAMP,
        paid_at TIMESTAMP,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        CONSTRAINT uq_booking_member_payout UNIQUE (booking_id, ensemble_member_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_member_payout_booking_id ON booking_member_payout (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_member_payout_ensemble_member_id ON booking_member_payout (ensemble_member_id)",
    # Unicidad de identificadores de cuenta / perfil
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_stage_name_lower
    ON musician_profile (lower(btrim(stage_name)))
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_phone_normalized
    ON "user" (phone)
    WHERE phone IS NOT NULL AND btrim(phone) <> ''
    """,
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS username VARCHAR',
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username_lower
    ON "user" (lower(btrim(username)))
    WHERE username IS NOT NULL AND btrim(username) <> ''
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_slug_lower
    ON musician_profile (lower(btrim(slug)))
    WHERE slug IS NOT NULL AND btrim(slug) <> ''
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_contractor_document_number_normalized
    ON contractor_profile (upper(replace(btrim(document_number), ' ', '')))
    WHERE document_number IS NOT NULL AND btrim(document_number) <> ''
    """,
    # Email verification & password reset on user
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMP',
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verification_token VARCHAR',
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verification_expires_at TIMESTAMP',
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR',
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS password_reset_expires_at TIMESTAMP',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email_verification_token ON "user" (email_verification_token) WHERE email_verification_token IS NOT NULL',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_password_reset_token ON "user" (password_reset_token) WHERE password_reset_token IS NOT NULL',
    """
    CREATE TABLE IF NOT EXISTS email_template (
        id UUID PRIMARY KEY,
        slug VARCHAR NOT NULL UNIQUE,
        name VARCHAR NOT NULL,
        description VARCHAR,
        subject VARCHAR NOT NULL,
        html_body TEXT NOT NULL,
        text_body TEXT NOT NULL,
        available_variables JSONB NOT NULL DEFAULT '[]'::jsonb,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_email_template_slug ON email_template (slug)",
    """
    CREATE TABLE IF NOT EXISTS email_log (
        id UUID PRIMARY KEY,
        template_slug VARCHAR NOT NULL,
        recipient VARCHAR NOT NULL,
        subject VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        error_message TEXT,
        provider_message_id VARCHAR,
        user_id UUID REFERENCES "user"(id),
        meta JSONB,
        created_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_email_log_template_slug ON email_log (template_slug)",
    "CREATE INDEX IF NOT EXISTS ix_email_log_recipient ON email_log (recipient)",
    "CREATE INDEX IF NOT EXISTS ix_email_log_status ON email_log (status)",
    "CREATE INDEX IF NOT EXISTS ix_email_log_created_at ON email_log (created_at)",
    'UPDATE "user" SET email_verified_at = created_at WHERE email_verified_at IS NULL',
    """
    DO $$ BEGIN
        CREATE TYPE bookingcomplaintstatus AS ENUM (
            'open', 'musician_accepted', 'musician_responded', 'settled'
        );
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS booking_complaint (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL UNIQUE REFERENCES booking(id),
        opened_by_user_id UUID NOT NULL REFERENCES "user"(id),
        reason TEXT NOT NULL,
        evidence_url VARCHAR,
        status bookingcomplaintstatus NOT NULL DEFAULT 'open',
        musician_response TEXT,
        musician_response_evidence_url VARCHAR,
        musician_responded_at TIMESTAMP,
        admin_musician_amount NUMERIC,
        admin_contractor_refund NUMERIC,
        admin_notes TEXT,
        settled_by_admin_id UUID REFERENCES "user"(id),
        settled_at TIMESTAMP,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_complaint_booking_id ON booking_complaint (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_complaint_status ON booking_complaint (status)",
    """
    CREATE TABLE IF NOT EXISTS platform_payment_settings (
        id UUID PRIMARY KEY,
        phone_number VARCHAR NOT NULL DEFAULT '',
        phone_label VARCHAR NOT NULL DEFAULT 'Yape / Plin',
        account_name VARCHAR,
        qr_image_url VARCHAR,
        instructions TEXT,
        updated_at TIMESTAMP,
        created_at TIMESTAMP
    )
    """,
    "ALTER TABLE platform_payment_settings ADD COLUMN IF NOT EXISTS platform_fee_percent NUMERIC NOT NULL DEFAULT 2",
    "ALTER TABLE platform_payment_settings ALTER COLUMN platform_fee_percent SET DEFAULT 2",
    # Seed initial commission for environments that still have the old 0% default.
    "UPDATE platform_payment_settings SET platform_fee_percent = 2 WHERE platform_fee_percent = 0",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS platform_fee_percent NUMERIC",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS platform_fee_amount NUMERIC",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_status VARCHAR NOT NULL DEFAULT 'none'",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_evidence_url VARCHAR",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_sent_at TIMESTAMP",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_sent_by_admin_id UUID REFERENCES \"user\"(id)",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_validated_at TIMESTAMP",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_rejection_reason TEXT",
    "ALTER TABLE booking_complaint ADD COLUMN IF NOT EXISTS refund_payment_id UUID REFERENCES payment(id)",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS evidence_urls JSONB",
    """
    UPDATE payment
    SET evidence_urls = jsonb_build_array(evidence_url)
    WHERE evidence_url IS NOT NULL
      AND evidence_url <> ''
      AND (evidence_urls IS NULL OR evidence_urls = 'null'::jsonb OR evidence_urls = '[]'::jsonb)
    """,
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS signature_image_url VARCHAR",
    "ALTER TABLE contract ADD COLUMN IF NOT EXISTS musician_signature_url VARCHAR",
    """
    CREATE TABLE IF NOT EXISTS booking_location_share (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL UNIQUE REFERENCES booking(id) ON DELETE CASCADE,
        musician_sharing BOOLEAN NOT NULL DEFAULT FALSE,
        contractor_sharing BOOLEAN NOT NULL DEFAULT FALSE,
        musician_requested_at TIMESTAMP,
        contractor_requested_at TIMESTAMP,
        musician_requested_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        contractor_requested_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        musician_lat DOUBLE PRECISION,
        musician_lng DOUBLE PRECISION,
        musician_accuracy DOUBLE PRECISION,
        musician_updated_at TIMESTAMP,
        contractor_lat DOUBLE PRECISION,
        contractor_lng DOUBLE PRECISION,
        contractor_accuracy DOUBLE PRECISION,
        contractor_updated_at TIMESTAMP,
        event_lat DOUBLE PRECISION,
        event_lng DOUBLE PRECISION,
        event_address VARCHAR,
        event_city VARCHAR,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_location_share_booking_id ON booking_location_share (booking_id)",
    """
    CREATE TABLE IF NOT EXISTS booking_location_ping (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        party VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        accuracy DOUBLE PRECISION,
        created_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_location_ping_booking_id ON booking_location_ping (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_location_ping_user_id ON booking_location_ping (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_location_ping_created_at ON booking_location_ping (created_at)",
    """
    CREATE TABLE IF NOT EXISTS booking_location_participant (
        id UUID PRIMARY KEY,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        role VARCHAR NOT NULL,
        display_name VARCHAR NOT NULL DEFAULT '',
        sharing BOOLEAN NOT NULL DEFAULT FALSE,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        accuracy DOUBLE PRECISION,
        updated_at TIMESTAMP,
        requested_at TIMESTAMP,
        requested_by_user_id UUID REFERENCES "user"(id) ON DELETE SET NULL,
        created_at TIMESTAMP,
        CONSTRAINT uq_booking_location_participant_booking_user UNIQUE (booking_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_location_participant_booking_id ON booking_location_participant (booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_location_participant_user_id ON booking_location_participant (user_id)",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS slug VARCHAR",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_slug
    ON musician_profile (slug)
    WHERE slug IS NOT NULL
    """,
    # Validación de comprobantes centralizada en admin (con auditoría).
    "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'rejected'",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS reviewed_by_user_id UUID REFERENCES \"user\"(id)",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR",
    # Aceptación de Términos y Condiciones al registrarse.
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS terms_accepted_ip VARCHAR",
    # Módulo de ayuda: comentarios de cualquier usuario (o visitante anónimo),
    # gestionados por el admin.
    """
    DO $$ BEGIN
        CREATE TYPE supportticketstatus AS ENUM ('open', 'in_progress', 'resolved');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS support_ticket (
        id UUID PRIMARY KEY,
        user_id UUID REFERENCES "user"(id),
        guest_name VARCHAR,
        guest_email VARCHAR,
        message TEXT NOT NULL,
        status supportticketstatus NOT NULL DEFAULT 'open',
        admin_response TEXT,
        responded_by_admin_id UUID REFERENCES "user"(id),
        responded_at TIMESTAMP,
        submitted_ip VARCHAR,
        submitted_user_agent VARCHAR,
        submitted_path VARCHAR,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_support_ticket_user_id ON support_ticket (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_support_ticket_status ON support_ticket (status)",
    'ALTER TABLE "user" ALTER COLUMN fullname DROP NOT NULL',
    "ALTER TABLE musician_profile ALTER COLUMN stage_name DROP NOT NULL",
    # Columnas para pasarela de pago (Mercado Pago).
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_provider VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_payment_id VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_preference_id VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS gateway_metadata JSONB",
    "CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_id ON payment (gateway_payment_id)",
    # Columnas para desembolso a músicos (Opción 2: Transferencia / Yape / Mercado Pago).
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_method VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_bank_name VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_account_number VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_cci VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_phone VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_beneficiary_name VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_beneficiary_document VARCHAR",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS payout_mp_email VARCHAR",
    # Columnas para liquidación y desembolso en pagos.
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS payout_reference VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS payout_evidence_url VARCHAR",
    "ALTER TABLE payment ADD COLUMN IF NOT EXISTS payout_notes VARCHAR",
    # Integracin con Google Calendar
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS calendar_event_id VARCHAR",
]



def run_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    if "user" not in inspector.get_table_names():
        return

    # Each statement gets its own transaction/savepoint so one failure
    # does not abort later migrations (PostgreSQL transaction abort).
    for statement in MIGRATION_STATEMENTS:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except Exception:
            # Best-effort migrations for dev environments.
            pass

    db: Session = SessionLocal()
    try:
        ensure_role_profiles(db)
        fix_unverified_published_profiles(db)
        ensure_email_templates(db)
        backfill_musician_slugs(db)
        backfill_user_usernames(db)
    finally:
        db.close()
