from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole
from app.services.musician_profiles import resolve_active_profile, user_has_other_published_profile
from app.services.uniqueness import next_available_musician_slug, next_available_stage_name


def get_or_create_musician_profile(db: Session, user: User) -> MusicianProfile:
    """El perfil activo de la persona (el elegido por la app o el principal); si no tiene ninguno, crea el principal."""
    profile = resolve_active_profile(db, user)
    if profile:
        return profile

    stage_name = next_available_stage_name(db, user.fullname)
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=stage_name,
        slug=next_available_musician_slug(db, stage_name),
        status=ProfileStatus.draft,
        availability_type=AvailabilityType.both,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def get_or_create_contractor_profile(db: Session, user: User) -> ContractorProfile:
    profile = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.user_id == user.id)
        .first()
    )
    if profile:
        return profile

    profile = ContractorProfile(
        user_id=user.id,
        status=ProfileStatus.draft,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _profile_values_equal(current, incoming) -> bool:
    if isinstance(current, list) or isinstance(incoming, list):
        left = list(current or [])
        right = list(incoming or [])
        return left == right
    if current is None and incoming in (None, ""):
        return True
    if incoming is None and current in (None, ""):
        return True
    try:
        from decimal import Decimal

        if isinstance(current, (int, float, Decimal)) or isinstance(
            incoming, (int, float, Decimal)
        ):
            return float(current) == float(incoming)
    except (TypeError, ValueError):
        pass
    return current == incoming


NON_DEMOTING_FIELDS = {
    "payout_method",
    "payout_bank_name",
    "payout_account_number",
    "payout_cci",
    "payout_phone",
    "payout_beneficiary_name",
    "payout_beneficiary_document",
    "payout_mp_email",
}


# Datos que la persona completa por su cuenta (tipo, tamaño del grupo y detalles): no obligan a revisar de nuevo.
SELF_SERVICE_FIELDS = frozenset({"profile_type", "group_size", "details"})


def demote_published_profile_if_changed(profile, updates: dict) -> bool:
    """Applies updates to the profile without demoting published profiles to manual review."""
    for field, value in updates.items():
        setattr(profile, field, value)
    return False


def demote_published_profile(profile) -> bool:
    """Media changes no longer demote a published profile to manual review."""
    return False


def auto_verify_musician_profile_if_complete(db: Session, profile: MusicianProfile) -> bool:
    """Si el perfil de músico tiene todos sus campos obligatorios completos, lo publica y verifica automáticamente."""
    from datetime import datetime
    from app.services.contract_pdf import (
        DEFAULT_CONTRACT_TEMPLATE_TITLE,
        build_default_contract_template_body,
        contract_html_text_length,
    )
    from app.services.profile_validation import validate_musician_profile
    from app.services.uniqueness import next_available_handle

    if getattr(profile, "is_ensemble_only", False):
        return False

    user = getattr(profile, "user", None) or db.get(User, profile.user_id)
    if user and not (user.username or "").strip() and (profile.stage_name or "").strip():
        user.username = profile.slug or next_available_handle(
            db,
            profile.stage_name,
            exclude_user_id=user.id,
            exclude_profile_id=profile.id,
        )

    if contract_html_text_length(profile.contract_template_body) < 50:
        profile.contract_template_title = profile.contract_template_title or DEFAULT_CONTRACT_TEMPLATE_TITLE
        profile.contract_template_body = build_default_contract_template_body()

    validation = validate_musician_profile(db, profile)
    if validation.can_submit:
        was_never_published = (
            profile.published_at is None
            and profile.status != ProfileStatus.published
        )
        now = datetime.utcnow()
        profile.status = ProfileStatus.published
        profile.submitted_at = profile.submitted_at or now
        profile.published_at = profile.published_at or now
        profile.rejection_reason = None
        if user is not None:
            user.is_verified = True
            if was_never_published and profile.id is not None:
                from app.services.booking_notifications import notify_profile_approved

                notify_profile_approved(
                    db,
                    user=user,
                    profile_role="musician",
                    profile_id=str(profile.id),
                )
        return True
    return False


def auto_verify_contractor_profile_if_complete(db: Session, profile: ContractorProfile) -> bool:
    """Si el perfil de contratista tiene todos sus campos obligatorios completos, lo publica y verifica automáticamente."""
    from datetime import datetime
    from app.services.profile_validation import validate_contractor_profile

    user = getattr(profile, "user", None) or db.get(User, profile.user_id)
    validation = validate_contractor_profile(profile)
    if validation.can_submit:
        was_never_published = (
            profile.published_at is None
            and profile.status != ProfileStatus.published
        )
        now = datetime.utcnow()
        profile.status = ProfileStatus.published
        profile.submitted_at = profile.submitted_at or now
        profile.published_at = profile.published_at or now
        profile.rejection_reason = None
        if user is not None:
            user.is_verified = True
            if was_never_published and profile.id is not None:
                from app.services.booking_notifications import notify_profile_approved

                notify_profile_approved(
                    db,
                    user=user,
                    profile_role="contractor",
                    profile_id=str(profile.id),
                )
        return True
    return False


def fix_unverified_published_profiles(db: Session) -> None:
    """Revert profiles that were published without admin approval."""
    musician_profiles = (
        db.query(MusicianProfile)
        .join(User)
        .filter(
            MusicianProfile.status == ProfileStatus.published,
            User.is_verified.is_(False),
        )
        .all()
    )
    for profile in musician_profiles:
        profile.status = ProfileStatus.draft
        profile.published_at = None

    contractor_profiles = (
        db.query(ContractorProfile)
        .join(User)
        .filter(
            ContractorProfile.status == ProfileStatus.published,
            User.is_verified.is_(False),
        )
        .all()
    )
    for profile in contractor_profiles:
        profile.status = ProfileStatus.draft
        profile.published_at = None

    if musician_profiles or contractor_profiles:
        db.commit()


def ensure_role_profiles(db: Session | None = None) -> None:
    owns_session = db is None
    if owns_session:
        db = SessionLocal()

    try:
        musicians = db.query(User).filter(User.role == UserRole.musician).all()
        for user in musicians:
            existing = (
                db.query(MusicianProfile)
                .filter(MusicianProfile.user_id == user.id)
                .first()
            )
            if existing:
                continue
            db.add(
                MusicianProfile(
                    user_id=user.id,
                    stage_name=user.fullname,
                    status=ProfileStatus.draft,
                    availability_type=AvailabilityType.both,
                )
            )

        contractors = db.query(User).filter(User.role == UserRole.contractor).all()
        for user in contractors:
            existing = (
                db.query(ContractorProfile)
                .filter(ContractorProfile.user_id == user.id)
                .first()
            )
            if existing:
                continue
            db.add(
                ContractorProfile(
                    user_id=user.id,
                    status=ProfileStatus.draft,
                )
            )

        db.commit()
    finally:
        if owns_session:
            db.close()


def backfill_musician_slugs(db: Session) -> None:
    """Asigna slug a perfiles de músico creados antes de que existiera la columna."""
    profiles = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.slug.is_(None))
        .all()
    )
    if not profiles:
        return
    for profile in profiles:
        profile.slug = next_available_musician_slug(
            db, profile.stage_name, exclude_profile_id=profile.id,
        )
    db.commit()


def backfill_user_usernames(db: Session) -> None:
    """Asigna username único a usuarios existentes que aún no tengan uno."""
    from app.services.uniqueness import normalize_username

    users = db.query(User).filter(User.username.is_(None)).all()
    if not users:
        return

    assigned = {
        u[0].strip().lower()
        for u in db.query(User.username).filter(User.username.isnot(None)).all()
        if u[0]
    }

    for user in users:
        candidate = ""
        if user.musician_profile and user.musician_profile.slug:
            candidate = normalize_username(user.musician_profile.slug)
        if not candidate:
            base = user.fullname or (user.email.split("@")[0] if user.email else "usuario")
            candidate = normalize_username(base)
        if not candidate or len(candidate) < 3:
            candidate = f"user-{str(user.id)[:8]}"

        slug = candidate
        counter = 2
        while slug in assigned:
            slug = f"{candidate}-{counter}"
            counter += 1

        assigned.add(slug)
        user.username = slug
        if user.role == UserRole.musician and user.musician_profile:
            user.musician_profile.slug = slug

    db.commit()
