import logging

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
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS google_calendar_refresh_token VARCHAR",

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
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP",
    """
    CREATE TABLE IF NOT EXISTS uploaded_file (
        id UUID PRIMARY KEY,
        filename VARCHAR NOT NULL UNIQUE,
        owner_user_id UUID NOT NULL REFERENCES "user"(id),
        content_type VARCHAR NOT NULL,
        size_bytes INTEGER,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_uploaded_file_owner_user_id ON uploaded_file (owner_user_id)",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_percent NUMERIC",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_amount NUMERIC",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_status VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refund_error VARCHAR",
    "ALTER TABLE booking ADD COLUMN IF NOT EXISTS cancellation_refunded_at TIMESTAMP",
    # Falla (y se registra) si ya hay pagos duplicados: limpiarlos y reiniciar.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_gateway_payment_id ON payment (gateway_payment_id) "
    "WHERE gateway_payment_id IS NOT NULL AND payment_type IS DISTINCT FROM 'refund'",
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
    "ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS ended_by VARCHAR(16)",
    "ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ",
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
    "DROP INDEX IF EXISTS uq_user_phone_normalized",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_phone_normalized
    ON "user" (phone)
    WHERE phone IS NOT NULL AND btrim(phone) <> '' AND email NOT LIKE '%@guest.local'
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
    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0',
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
    """
    UPDATE notification
    SET 
        title = replace(replace(replace(replace(replace(replace(replace(replace(title, 'á', 'á'), 'é', 'é'), 'í', 'í'), 'ó', 'ó'), 'ú', 'ú'), 'ñ', 'ñ'), '¡', '¡'), '¿', '¿'),
        message = replace(replace(replace(replace(replace(replace(replace(replace(message, 'á', 'á'), 'é', 'é'), 'í', 'í'), 'ó', 'ó'), 'ú', 'ú'), 'ñ', 'ñ'), '¡', '¡'), '¿', '¿')
    WHERE title LIKE '%Ã%' OR message LIKE '%Ã%' OR title LIKE '%Â%' OR message LIKE '%Â%';
    """,
    # Integrantes
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS is_ensemble_only BOOLEAN NOT NULL DEFAULT FALSE",
    # Tipo de perfil (solista o grupo) y datos de detalle (app/services/profile_details.py).
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS profile_type VARCHAR(16) NOT NULL DEFAULT 'solo'",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS group_size VARCHAR(8)",
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb",
    """
    UPDATE musician_profile AS mp
    SET profile_type = 'group'
    WHERE mp.profile_type = 'solo'
      AND EXISTS (
          SELECT 1 FROM ensemble_member AS em
          WHERE em.leader_user_id = mp.user_id
            AND em.status::text IN ('invited', 'active')
      )
    """,
    # Varios perfiles por persona: se quita la unicidad de user_id y se marca uno como principal.
    "ALTER TABLE musician_profile ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT TRUE",
    """
    DO $$
    DECLARE r RECORD;
    BEGIN
        FOR r IN
            SELECT c.conname FROM pg_constraint c
            WHERE c.conrelid = 'musician_profile'::regclass AND c.contype = 'u'
              AND array_length(c.conkey, 1) = 1
              AND c.conkey[1] = (SELECT attnum FROM pg_attribute WHERE attrelid = 'musician_profile'::regclass AND attname = 'user_id')
        LOOP
            EXECUTE format('ALTER TABLE musician_profile DROP CONSTRAINT %I', r.conname);
        END LOOP;
        FOR r IN
            SELECT i.indexrelid::regclass::text AS idx
            FROM pg_index i
            WHERE i.indrelid = 'musician_profile'::regclass AND i.indisunique AND NOT i.indisprimary
              AND i.indpred IS NULL AND i.indnatts = 1
              AND i.indkey[0] = (SELECT attnum FROM pg_attribute WHERE attrelid = 'musician_profile'::regclass AND attname = 'user_id')
              AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
        LOOP
            EXECUTE format('DROP INDEX %s', r.idx);
        END LOOP;
    END $$;
    """,
    "CREATE INDEX IF NOT EXISTS ix_musician_profile_user_id ON musician_profile (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_musician_profile_one_primary ON musician_profile (user_id) WHERE is_primary",
    # Integrantes por perfil (grupo): los anteriores quedan en el perfil principal de su líder.
    "ALTER TABLE ensemble_member ADD COLUMN IF NOT EXISTS profile_id UUID REFERENCES musician_profile(id) ON DELETE CASCADE",
    "CREATE INDEX IF NOT EXISTS ix_ensemble_member_profile_id ON ensemble_member (profile_id)",
    """
    UPDATE ensemble_member AS em
    SET profile_id = mp.id
    FROM musician_profile AS mp
    WHERE em.profile_id IS NULL AND mp.user_id = em.leader_user_id AND mp.is_primary
    """,
    "ALTER TABLE ensemble_member DROP CONSTRAINT IF EXISTS uq_ensemble_member_leader_email",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ensemble_member_profile_email ON ensemble_member (profile_id, email) WHERE profile_id IS NOT NULL",

    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_hash VARCHAR",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_expires_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_last_sent_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_sent_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS login_otp_window_started_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS email_verification_sent_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS email_verification_sent_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS email_verification_window_started_at TIMESTAMP",
    "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS created_by_musician_id UUID",
    """
    DO $$ BEGIN
        CREATE TYPE bookinglinkstatus AS ENUM ('active', 'completed', 'expired');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS booking_link (
        id UUID PRIMARY KEY,
        token VARCHAR NOT NULL UNIQUE,
        musician_id UUID NOT NULL REFERENCES musician_profile(id),
        price NUMERIC NOT NULL,
        event_date DATE,
        start_time TIME,
        client_email VARCHAR,
        status bookinglinkstatus NOT NULL DEFAULT 'active',
        expires_at TIMESTAMP NOT NULL,
        booking_id UUID REFERENCES booking(id),
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_booking_link_token ON booking_link (token)",
    """
    CREATE TABLE IF NOT EXISTS payout_request (
        id UUID PRIMARY KEY,
        musician_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        amount NUMERIC NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'pending',
        notes VARCHAR,
        created_at TIMESTAMP DEFAULT NOW(),
        processed_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_payout_request_musician_id ON payout_request (musician_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_payout_request_musician_pending
    ON payout_request (musician_id)
    WHERE status = 'pending'
    """,
    """
    CREATE TABLE IF NOT EXISTS payout_request_booking (
        payout_request_id UUID NOT NULL REFERENCES payout_request(id) ON DELETE CASCADE,
        booking_id UUID NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT NOW(),
        PRIMARY KEY (payout_request_id, booking_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_payout_request_booking_booking_id ON payout_request_booking (booking_id)",
    # Pre-llenado completo en booking_link (desde asistente de WhatsApp o autocompletado por audio/texto)
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS end_time TIME",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS event_type VARCHAR(120)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_address VARCHAR(300)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_city VARCHAR(120)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS location_reference VARCHAR(255)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_fullname VARCHAR(120)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_phone VARCHAR(40)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS client_document_number VARCHAR(20)",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS event_description TEXT",
    "ALTER TABLE booking_link ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'manual'",
    # Asistente IA por perfil de músico y canal WhatsApp Business (Meta Cloud API)
    """
    CREATE TABLE IF NOT EXISTS musician_assistant_config (
        id UUID PRIMARY KEY,
        profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        greeting_message VARCHAR(500),
        packages JSONB NOT NULL DEFAULT '[]'::jsonb,
        additional_hour_price NUMERIC(10, 2),
        zone_surcharges JSONB NOT NULL DEFAULT '[]'::jsonb,
        per_km_rate NUMERIC(10, 2),
        night_surcharge_amount NUMERIC(10, 2) DEFAULT 0,
        night_surcharge_start_hour INTEGER NOT NULL DEFAULT 23,
        special_date_surcharge_percent NUMERIC(5, 2) DEFAULT 0,
        max_discount_amount NUMERIC(10, 2) DEFAULT 0,
        max_discount_percent NUMERIC(5, 2) DEFAULT 0,
        custom_instructions TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_assistant_config_profile_id ON musician_assistant_config (profile_id)",
    """
    CREATE TABLE IF NOT EXISTS musician_whatsapp_channel (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        phone_number_id VARCHAR(64),
        waba_id VARCHAR(64),
        display_phone_number VARCHAR(40),
        access_token_encrypted TEXT,
        webhook_verify_token VARCHAR(128),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        auto_pause_on_human_reply BOOLEAN NOT NULL DEFAULT TRUE,
        ignore_ensemble_members BOOLEAN NOT NULL DEFAULT TRUE,
        ignore_personal_chats BOOLEAN NOT NULL DEFAULT TRUE,
        ignored_phones JSONB NOT NULL DEFAULT '[]'::jsonb,
        connected_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_whatsapp_channel_user_id ON musician_whatsapp_channel (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_musician_whatsapp_channel_phone_number_id ON musician_whatsapp_channel (phone_number_id)",
    """
    CREATE TABLE IF NOT EXISTS assistant_conversation (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        profile_id UUID REFERENCES musician_profile(id) ON DELETE SET NULL,
        channel VARCHAR(24) NOT NULL DEFAULT 'whatsapp',
        client_phone VARCHAR(40),
        client_name VARCHAR(120),
        status VARCHAR(32) NOT NULL DEFAULT 'bot_active',
        escalation_reason VARCHAR(255),
        extracted_data JSONB NOT NULL DEFAULT '{}'::jsonb,
        messages JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_booking_link_id UUID REFERENCES booking_link(id) ON DELETE SET NULL,
        last_interaction_id VARCHAR(128),
        last_message_at TIMESTAMPTZ DEFAULT NOW(),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_user_id ON assistant_conversation (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_profile_id ON assistant_conversation (profile_id)",
    "CREATE INDEX IF NOT EXISTS ix_assistant_conversation_client_phone ON assistant_conversation (client_phone)",
    # Topes de uso del asistente (contadores atómicos + límites que ajusta el admin)
    """
    CREATE TABLE IF NOT EXISTS assistant_usage_counter (
        key VARCHAR(120) PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assistant_platform_settings (
        id UUID PRIMARY KEY,
        monthly_ai_calls_per_musician INTEGER NOT NULL DEFAULT 300,
        monthly_audio_per_musician INTEGER NOT NULL DEFAULT 60,
        daily_ai_calls_global INTEGER NOT NULL DEFAULT 3000,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
]

# Búsqueda semántica del asistente IA (pgvector). Tolerante: sin la extensión no falla y el asistente sigue
# funcionando sin búsqueda semántica. Mismo SQL que `alembic/versions/20261117_b8c9d0e1f2a3_*`.
PGVECTOR_STATEMENTS = [
    """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            BEGIN
                CREATE EXTENSION IF NOT EXISTS vector;
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'No se pudo crear la extension vector';
            END;
        END IF;
    END
    $$
    """,
    """
    DO $$
    DECLARE
        ext_schema text;
    BEGIN
        SELECT n.nspname INTO ext_schema
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector';
        IF ext_schema IS NOT NULL THEN
            -- El tipo `vector` vive en el esquema donde se instaló la extensión (no siempre el de la sesión).
            EXECUTE $q$
                CREATE TABLE IF NOT EXISTS assistant_knowledge_chunk (
                    id UUID PRIMARY KEY,
                    profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
                    source VARCHAR(32) NOT NULL,
                    source_ref VARCHAR(120) NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    content_hash VARCHAR(64) NOT NULL,
                    embedding $q$ || quote_ident(ext_schema) || $q$.vector(768) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            $q$;
            CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_chunk_profile
                ON assistant_knowledge_chunk (profile_id);
            EXECUTE $q$
                CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_chunk_embedding
                ON assistant_knowledge_chunk USING hnsw (embedding $q$ || quote_ident(ext_schema) || $q$.vector_cosine_ops)
            $q$;
        END IF;
    END
    $$
    """,
]
MIGRATION_STATEMENTS += PGVECTOR_STATEMENTS


# Entrenamiento del asistente IA: tono, voz, reglas de derivación, conocimiento, dudas pendientes y versiones.
# Mismo SQL que `alembic/versions/20261117_c9d0e1f2a3b4_*`.
ASSISTANT_TRAINING_STATEMENTS = [
    "ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS tone VARCHAR(16) NOT NULL DEFAULT 'cercano'",
    "ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS voice_examples JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE musician_assistant_config ADD COLUMN IF NOT EXISTS escalation_rules JSONB NOT NULL DEFAULT '{}'::jsonb",
    """
    CREATE TABLE IF NOT EXISTS assistant_knowledge_entry (
        id UUID PRIMARY KEY,
        profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        question VARCHAR(300) NOT NULL,
        answer TEXT NOT NULL,
        source VARCHAR(24) NOT NULL DEFAULT 'tu',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_knowledge_entry_profile_id ON assistant_knowledge_entry (profile_id)",
    """
    CREATE TABLE IF NOT EXISTS assistant_pending_question (
        id UUID PRIMARY KEY,
        profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        conversation_id UUID REFERENCES assistant_conversation(id) ON DELETE SET NULL,
        question VARCHAR(300) NOT NULL,
        context TEXT,
        asked_count INTEGER NOT NULL DEFAULT 1,
        status VARCHAR(16) NOT NULL DEFAULT 'open',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        answered_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_pending_question_profile_id ON assistant_pending_question (profile_id)",
    """
    CREATE TABLE IF NOT EXISTS assistant_config_version (
        id UUID PRIMARY KEY,
        profile_id UUID NOT NULL REFERENCES musician_profile(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        label VARCHAR(255) NOT NULL DEFAULT '',
        snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        CONSTRAINT uq_assistant_config_version_profile_number UNIQUE (profile_id, number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_config_version_profile_id ON assistant_config_version (profile_id)",
]
MIGRATION_STATEMENTS += ASSISTANT_TRAINING_STATEMENTS


_STARTUP_SEED_ADVISORY_LOCK_KEY = 984512301


def run_startup_seeds() -> None:
    """Ejecuta seeds y backfills idempotentes una sola vez (con advisory lock en Postgres)."""
    db: Session = SessionLocal()
    locked = False
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            locked = bool(
                db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _STARTUP_SEED_ADVISORY_LOCK_KEY},
                ).scalar()
            )
            if not locked:
                return
        ensure_role_profiles(db)
        ensure_email_templates(db)
        backfill_musician_slugs(db)
        backfill_user_usernames(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        if locked:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _STARTUP_SEED_ADVISORY_LOCK_KEY},
                )
            except Exception:
                pass
        db.close()


def run_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    if "user" not in inspector.get_table_names():
        from app.db.session import Base
        Base.metadata.create_all(bind=engine)

    logger = logging.getLogger(__name__)
    # Each statement gets its own transaction/savepoint so one failure
    # does not abort later migrations (PostgreSQL transaction abort).
    for statement in MIGRATION_STATEMENTS:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except Exception as exc:
            logger.warning("Migration statement failed: %s", exc)

    run_startup_seeds()
