from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.contractor_profile import ContractorProfile
from app.models.musician_availability import MusicianAvailability
from app.models.musician_media import MusicianMedia, MediaType
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.services.contract_pdf import contract_html_text_length
from app.services.profile_visibility import is_profile_public


@dataclass
class ValidationStep:
    id: int
    key: str
    label: str
    completed: bool
    missing: list[str] = field(default_factory=list)
    required: bool = True


@dataclass
class ProfileValidationResult:
    status: ProfileStatus
    current_step: int
    steps: list[ValidationStep]
    can_submit: bool
    is_public: bool
    is_ensemble_only: bool = False


def _has_price(profile: MusicianProfile) -> bool:
    if profile.availability_type == AvailabilityType.hourly:
        return profile.price_per_hour is not None
    if profile.availability_type == AvailabilityType.per_event:
        return profile.price_per_event is not None
    return profile.price_per_hour is not None or profile.price_per_event is not None


def _has_social_link(profile: MusicianProfile) -> bool:
    links = [
        profile.instagram_url,
        profile.facebook_url,
        profile.tiktok_url,
        profile.youtube_channel_url,
        profile.spotify_url,
        profile.website_url,
    ]
    return any(link and link.strip() for link in links)


def validate_musician_profile(db: Session, profile: MusicianProfile) -> ProfileValidationResult:
    image_media_count = (
        db.query(MusicianMedia)
        .filter(
            MusicianMedia.musician_id == profile.id,
            MusicianMedia.type == MediaType.image,
        )
        .count()
    )
    video_media_count = (
        db.query(MusicianMedia)
        .filter(
            MusicianMedia.musician_id == profile.id,
            MusicianMedia.type == MediaType.video,
        )
        .count()
    )
    availability_count = (
        db.query(MusicianAvailability)
        .filter(MusicianAvailability.musician_id == profile.id)
        .count()
    )
    video_urls = [url for url in (profile.videos or []) if url and url.strip()]

    identity_missing: list[str] = []
    user = getattr(profile, "user", None)
    if not user or not (user.username or "").strip():
        identity_missing.append("username")
    if not user or not (user.fullname or "").strip():
        identity_missing.append("fullname")
    if not profile.stage_name or not profile.stage_name.strip():
        identity_missing.append("stage_name")
    if not profile.bio or len(profile.bio.strip()) < 40:
        identity_missing.append("bio")
    if not profile.profile_image_url and image_media_count == 0:
        identity_missing.append("profile_image_url")

    specialty_missing: list[str] = []
    if not profile.genres:
        specialty_missing.append("genres")
    if not profile.instruments:
        specialty_missing.append("instruments")
    if not profile.availability_type:
        specialty_missing.append("availability_type")

    location_missing: list[str] = []
    if not profile.location_city or not profile.location_city.strip():
        location_missing.append("location_city")
    if not profile.location_zone or not profile.location_zone.strip():
        location_missing.append("location_zone")
    if not _has_price(profile):
        location_missing.append("price")

    repertoire_missing: list[str] = []
    repertoire = getattr(profile, "repertoire", None) or []
    if not repertoire and not profile.songs:
        repertoire_missing.append("songs")

    media_missing: list[str] = []
    if image_media_count < 1 and not profile.profile_image_url:
        media_missing.append("media_images")
    if video_media_count < 1 and len(video_urls) < 1:
        media_missing.append("videos")

    social_missing: list[str] = []
    if not _has_social_link(profile):
        social_missing.append("social_links")

    availability_missing: list[str] = []
    if availability_count < 1:
        availability_missing.append("availability")

    documents_missing: list[str] = []
    if not profile.id_document_url:
        documents_missing.append("id_document_url")

    contract_missing: list[str] = []
    if contract_html_text_length(profile.contract_template_body) < 50:
        contract_missing.append("contract_template_body")
    if not (profile.signature_image_url or "").strip():
        contract_missing.append("signature_image_url")

    steps = [
        ValidationStep(
            1,
            "identity",
            "Identidad artística",
            not identity_missing,
            identity_missing,
            required=True,
        ),
        ValidationStep(
            2,
            "specialty",
            "Especialidad",
            not specialty_missing,
            specialty_missing,
            required=True,
        ),
        ValidationStep(
            3,
            "location_pricing",
            "Ubicación y tarifas",
            not location_missing,
            location_missing,
            required=False,
        ),
        ValidationStep(
            4,
            "repertoire",
            "Repertorio",
            not repertoire_missing,
            repertoire_missing,
            required=False,
        ),
        ValidationStep(
            5,
            "media",
            "Fotos y videos",
            not media_missing,
            media_missing,
            required=False,
        ),
        ValidationStep(
            6,
            "social",
            "Redes sociales",
            not social_missing,
            social_missing,
            required=False,
        ),
        ValidationStep(
            7,
            "availability",
            "Disponibilidad",
            not availability_missing,
            availability_missing,
            required=False,
        ),
        ValidationStep(
            8,
            "documents",
            "Documento de identidad",
            not documents_missing,
            documents_missing,
            required=True,
        ),
        ValidationStep(
            9,
            "contract",
            "Plantilla y firma de contrato",
            not contract_missing,
            contract_missing,
            required=False,
        ),
    ]

    required_steps = [step for step in steps if step.required]
    current_step = next(
        (step.id for step in required_steps if not step.completed),
        next((step.id for step in steps if not step.completed), len(steps) + 1),
    )
    can_submit = all(step.completed for step in required_steps)

    return ProfileValidationResult(
        status=profile.status,
        current_step=current_step,
        steps=steps,
        can_submit=can_submit,
        # Verificado/publicado = puede usar la app. El marketplace exige
        # todas las fases vía is_musician_listed_publicly().
        is_public=is_profile_public(profile),
        is_ensemble_only=getattr(profile, 'is_ensemble_only', False),
    )


def validate_contractor_profile(profile: ContractorProfile) -> ProfileValidationResult:
    personal_missing: list[str] = []
    user = getattr(profile, "user", None)
    if not user or not getattr(user, "fullname", None) or not user.fullname.strip():
        personal_missing.append("fullname")
    if not profile.document_type or not profile.document_type.strip():
        personal_missing.append("document_type")
    if not profile.document_number or not profile.document_number.strip():
        personal_missing.append("document_number")
    if not profile.address or not profile.address.strip():
        personal_missing.append("address")
    if not profile.city or not profile.city.strip():
        personal_missing.append("city")

    documents_missing: list[str] = []
    if not profile.id_document_url:
        documents_missing.append("id_document_url")

    steps = [
        ValidationStep(
            1,
            "personal",
            "Datos personales",
            not personal_missing,
            personal_missing,
            required=True,
        ),
        ValidationStep(
            2,
            "documents",
            "Documento de identidad",
            not documents_missing,
            documents_missing,
            required=True,
        ),
    ]

    current_step = next((step.id for step in steps if not step.completed), len(steps) + 1)
    can_submit = all(step.completed for step in steps)

    return ProfileValidationResult(
        status=profile.status,
        current_step=current_step,
        steps=steps,
        can_submit=can_submit,
        is_public=is_profile_public(profile),
        is_ensemble_only=getattr(profile, 'is_ensemble_only', False),
    )
