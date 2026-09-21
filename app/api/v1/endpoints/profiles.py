from datetime import datetime
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.api.profile_helpers import (
    demote_published_profile_if_changed,
    get_or_create_contractor_profile,
    get_or_create_musician_profile,
)
from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.contractor_recommendation import ContractorRecommendation
from app.models.user import User, UserRole
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.musician_media import MusicianMedia, MediaType
from app.models.contractor_profile import ContractorProfile
from app.models.profile_status import ProfileStatus
from app.schemas.profiles import (
    MusicianProfileCreate,
    MusicianProfileUpdate,
    MusicianProfileOut,
    ContractorProfileCreate,
    ContractorProfileUpdate,
    ContractorProfileOut,
    ContractorProfilePublicOut,
    ContractorPublicRecommendationOut,
    MusicianProfileListOut,
    MusicianProfilePublicOut,
    MusicianPublicReviewOut,
    ProfileValidationOut,
    ValidationStepOut,
    MusicianContractGenerateOut,
    ContractorContractGenerateOut,
    PlatformStatsOut,
)
from app.services.contract_pdf import contract_html_text_length, generate_musician_contract_pdf
from app.services.booking_notifications import (
    notify_profile_needs_resubmit,
    notify_profile_submitted,
)
from app.services.profile_validation import (
    validate_contractor_profile,
    validate_musician_profile,
)
from app.services.profile_visibility import is_musician_listed_publicly
from app.services.repertoire import prepare_musician_updates, repertoire_from_profile
from app.services.uniqueness import (
    assert_document_number_unique,
    assert_phone_unique,
    assert_stage_name_unique,
    assert_username_unique,
    next_available_musician_slug,
)

router = APIRouter(prefix="/profiles", tags=["Profiles"])

@router.get("/check-username")
def check_username(
    username: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    from app.models.user import User
    from app.utils.validators import normalize_username
    import re
    
    try:
        norm = normalize_username(username)
        existing = db.query(User).filter(User.username == norm, User.id != current_user.id).first()
        if existing:
            return {"available": False, "message": "El nombre de usuario ya está en uso."}
        return {"available": True, "message": "Nombre de usuario disponible.", "normalized": norm}
    except ValueError as e:
        return {"available": False, "message": str(e)}

@router.get("/reviews/latest", response_model=list[MusicianPublicReviewOut])
def get_latest_platform_reviews(
    db: Session = Depends(deps.get_db),
    limit: int = 6
):
    """Devuelve las reseñas más recientes de toda la plataforma."""
    rows = (
        db.query(BookingReview, Booking)
        .join(Booking, Booking.id == BookingReview.booking_id)
        .options(joinedload(BookingReview.author))
        .filter(
            BookingReview.is_final.is_(True),
            BookingReview.comment.isnot(None),
        )
        .order_by(BookingReview.created_at.desc())
        .limit(limit)
        .all()
    )
    reviews: list[MusicianPublicReviewOut] = []
    for review, booking in rows:
        author_label = "Anónimo"
        if review.guest_name:
            author_label = review.guest_name
        elif review.author and review.author.fullname:
            author_label = review.author.fullname.split(" ")[0]
        reviews.append(
            MusicianPublicReviewOut(
                id=review.id,
                rating=review.rating,
                comment=review.comment or "",
                author_label=author_label,
                event_type=booking.event_type,
                created_at=review.created_at,
            )
        )
    return reviews




def _published_musician_query(db: Session):
    return (
        db.query(MusicianProfile)
        .join(User)
        .options(joinedload(MusicianProfile.user))
        .filter(
            MusicianProfile.status == ProfileStatus.published,
            User.is_verified.is_(True),
        )
    )


def _to_musician_list_out(db: Session, profile: MusicianProfile) -> MusicianProfileListOut:
    repertoire = repertoire_from_profile(profile)
    gallery = _musician_gallery_images(db, profile)
    # Ensure profile photo is first in the carousel when present.
    if profile.profile_image_url and profile.profile_image_url not in gallery:
        gallery = [profile.profile_image_url, *gallery]
    elif profile.profile_image_url and gallery and gallery[0] != profile.profile_image_url:
        gallery = [profile.profile_image_url, *[u for u in gallery if u != profile.profile_image_url]]

    return MusicianProfileListOut(
        id=profile.id,
        stage_name=profile.stage_name,
        slug=profile.slug,
        bio=profile.bio,
        genres=profile.genres or [],
        instruments=profile.instruments or [],
        songs=[item["title"] for item in repertoire] or (profile.songs or []),
        repertoire=repertoire,
        price_per_hour=float(profile.price_per_hour) if profile.price_per_hour is not None else None,
        price_per_event=float(profile.price_per_event) if profile.price_per_event is not None else None,
        availability_type=profile.availability_type,
        location_city=profile.location_city,
        location_zone=profile.location_zone,
        rating_avg=float(profile.rating_avg) if profile.rating_avg is not None else None,
        rating_count=profile.rating_count,
        profile_image_url=profile.profile_image_url,
        gallery_images=gallery,
        videos=profile.videos or [],
        showreel_video_url=getattr(profile, "showreel_video_url", None),
        is_verified=profile.user.is_verified,
    )


def _musician_public_reviews(
    db: Session, musician_id, *, limit: int = 20
) -> list[MusicianPublicReviewOut]:
    rows = (
        db.query(BookingReview, Booking)
        .join(Booking, Booking.id == BookingReview.booking_id)
        .options(joinedload(BookingReview.author))
        .filter(
            Booking.musician_id == musician_id,
            BookingReview.is_final.is_(True),
            BookingReview.comment.isnot(None),
        )
        .order_by(BookingReview.created_at.desc())
        .limit(limit)
        .all()
    )
    reviews: list[MusicianPublicReviewOut] = []
    for review, booking in rows:
        author_label = "Cliente verificado"
        if review.author and review.author.fullname:
            author_label = review.author.fullname.split(" ")[0]
        reviews.append(
            MusicianPublicReviewOut(
                id=review.id,
                rating=review.rating,
                comment=review.comment or "",
                author_label=author_label,
                event_type=booking.event_type,
                created_at=review.created_at,
            )
        )
    return reviews


def _musician_gallery_images(db: Session, profile: MusicianProfile) -> list[str]:
    """Prefer uploaded media gallery; fall back to legacy profile.gallery_images."""
    media_urls = [
        row.url
        for row in (
            db.query(MusicianMedia)
            .filter(
                MusicianMedia.musician_id == profile.id,
                MusicianMedia.type == MediaType.image,
            )
            .order_by(MusicianMedia.order_index.asc(), MusicianMedia.created_at.asc())
            .all()
        )
        if row.url
    ]
    if media_urls:
        return media_urls
    return list(profile.gallery_images or [])


def _to_musician_public_out(
    db: Session, profile: MusicianProfile
) -> MusicianProfilePublicOut:
    repertoire = repertoire_from_profile(profile)
    return MusicianProfilePublicOut(
        id=profile.id,
        stage_name=profile.stage_name,
        slug=profile.slug,
        bio=profile.bio,
        genres=profile.genres or [],
        instruments=profile.instruments or [],
        songs=[item["title"] for item in repertoire] or (profile.songs or []),
        repertoire=repertoire,
        price_per_hour=float(profile.price_per_hour) if profile.price_per_hour is not None else None,
        price_per_event=float(profile.price_per_event) if profile.price_per_event is not None else None,
        portfolio_description=profile.portfolio_description,
        location_city=profile.location_city,
        location_zone=profile.location_zone,
        availability_type=profile.availability_type,
        rating_avg=float(profile.rating_avg) if profile.rating_avg is not None else None,
        rating_count=profile.rating_count,
        profile_image_url=profile.profile_image_url,
        gallery_images=_musician_gallery_images(db, profile),
        videos=profile.videos or [],
        showreel_video_url=getattr(profile, "showreel_video_url", None),
        instagram_url=profile.instagram_url,
        facebook_url=profile.facebook_url,
        tiktok_url=profile.tiktok_url,
        youtube_channel_url=profile.youtube_channel_url,
        spotify_url=profile.spotify_url,
        website_url=profile.website_url,
        is_verified=profile.user.is_verified,
        reviews=_musician_public_reviews(db, profile.id),
    )


def _validation_out(result) -> ProfileValidationOut:
    return ProfileValidationOut(
        status=result.status,
        current_step=result.current_step,
        steps=[
            ValidationStepOut(
                id=step.id,
                key=step.key,
                label=step.label,
                completed=step.completed,
                missing=step.missing,
                required=getattr(step, "required", True),
            )
            for step in result.steps
        ],
        can_submit=result.can_submit,
        is_public=result.is_public,
    )


def _get_musician_profile_or_404(db: Session, current_user: User) -> MusicianProfile:
    profile = get_or_create_musician_profile(db, current_user)
    return profile


def _get_contractor_profile_or_404(db: Session, current_user: User) -> ContractorProfile:
    profile = get_or_create_contractor_profile(db, current_user)
    return profile


# =========================
#       PLATFORM STATS
# =========================

@router.get(
    "/stats",
    response_model=PlatformStatsOut,
    status_code=status.HTTP_200_OK,
)
def get_platform_stats(db: Session = Depends(deps.get_db)):
    """Cifras reales y públicas para la landing (sin datos inventados)."""
    candidates = _published_musician_query(db).all()
    listed = [p for p in candidates if is_musician_listed_publicly(db, p)]

    ratings = [float(p.rating_avg) for p in listed if p.rating_avg is not None]
    average_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

    completed_bookings_count = (
        db.query(Booking).filter(Booking.status == BookingStatus.completed).count()
    )

    return PlatformStatsOut(
        musicians_count=len(listed),
        completed_bookings_count=completed_bookings_count,
        average_rating=average_rating,
    )


# =========================
#       MUSICIAN
# =========================

@router.get(
    "/musicians",
    response_model=List[MusicianProfileListOut],
    status_code=status.HTTP_200_OK,
)
def list_musician_profiles(
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(deps.get_db),
):
    candidates = _published_musician_query(db).all()
    listed = [
        profile
        for profile in candidates
        if is_musician_listed_publicly(db, profile)
    ]
    page = listed[skip : skip + limit]
    return [_to_musician_list_out(db, profile) for profile in page]


@router.get(
    "/musician/me",
    response_model=MusicianProfileOut,
    status_code=status.HTTP_200_OK,
)
def get_my_musician_profile(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo los músicos pueden acceder a su perfil de músico",
        )
    return _get_musician_profile_or_404(db, current_user)


@router.get(
    "/musician/me/status",
    response_model=ProfileValidationOut,
    status_code=status.HTTP_200_OK,
)
def get_my_musician_profile_status(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Solo los músicos pueden acceder")
    profile = _get_musician_profile_or_404(db, current_user)
    return _validation_out(validate_musician_profile(db, profile))


@router.post(
    "/musician/submit",
    response_model=MusicianProfileOut,
    status_code=status.HTTP_200_OK,
)
def submit_musician_profile(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Solo los músicos pueden enviar su perfil")

    profile = _get_musician_profile_or_404(db, current_user)
    validation = validate_musician_profile(db, profile)
    if not validation.can_submit:
        missing = [
            field
            for step in validation.steps
            if step.required
            for field in step.missing
        ]
        raise HTTPException(
            status_code=400,
            detail=f"Completa tu perfil antes de enviarlo. Faltan: {', '.join(missing)}",
        )

    profile.status = ProfileStatus.pending_review
    profile.submitted_at = datetime.utcnow()
    profile.rejection_reason = None
    notify_profile_submitted(
        db,
        user=current_user,
        profile_role="musician",
        profile_id=str(profile.id),
        display_name=profile.stage_name or current_user.fullname,
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.post(
    "/musician/contract/generate",
    response_model=MusicianContractGenerateOut,
    status_code=status.HTTP_200_OK,
)
def generate_my_musician_contract(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Solo los músicos pueden generar contratos")

    profile = _get_musician_profile_or_404(db, current_user)
    if contract_html_text_length(profile.contract_template_body) < 50:
        raise HTTPException(
            status_code=400,
            detail="Define la plantilla de contrato antes de generar el PDF",
        )

    pdf_url = generate_musician_contract_pdf(profile, current_user)
    profile.contract_pdf_url = pdf_url
    db.commit()
    return MusicianContractGenerateOut(contract_pdf_url=pdf_url)


@router.post(
    "/contractor/contract/generate",
    response_model=ContractorContractGenerateOut,
    status_code=status.HTTP_200_OK,
)
def generate_my_contractor_contract(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    raise HTTPException(
        status_code=403,
        detail="Solo los músicos pueden diseñar plantillas PDF de contrato",
    )


@router.post(
    "/musician",
    response_model=MusicianProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_musician_profile(
    payload: MusicianProfileCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo usuarios con rol 'musician' pueden crear este perfil",
        )

    existing = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.user_id == current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El perfil de músico ya existe. Debes actualizarlo.",
        )

    data = prepare_musician_updates(payload.model_dump())
    if data.get("availability_type") is None:
        data["availability_type"] = AvailabilityType.both

    if "stage_name" in data:
        stage_name = assert_stage_name_unique(db, data.get("stage_name"))
        if not stage_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El nombre de artista es obligatorio",
            )
        data["stage_name"] = stage_name
        data["slug"] = next_available_musician_slug(db, stage_name)

    profile = MusicianProfile(
        user_id=current_user.id,
        status=ProfileStatus.draft,
        **data,
    )

    db.add(profile)
    db.commit()
    db.refresh(profile)

    return profile


@router.put(
    "/musician",
    response_model=MusicianProfileOut,
    status_code=status.HTTP_200_OK,
)
def update_musician_profile(
    payload: MusicianProfileUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo usuarios con rol 'musician' pueden editar este perfil",
        )

    profile = _get_musician_profile_or_404(db, current_user)

    updates = prepare_musician_updates(payload.model_dump(exclude_unset=True))

    new_username_val: str | None = None
    if "username" in updates:
        raw_username = updates.pop("username")
        if raw_username is not None and raw_username.strip():
            new_username_val = assert_username_unique(
                db,
                raw_username,
                exclude_user_id=current_user.id,
            )
            current_user.username = new_username_val
            profile.slug = new_username_val

    if "fullname" in updates:
        raw_fullname = updates.pop("fullname")
        if raw_fullname is not None:
            clean_fullname = raw_fullname.strip()
            if len(clean_fullname) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El nombre completo debe tener al menos 2 caracteres",
                )
            current_user.fullname = clean_fullname

    if "stage_name" in updates:
        raw_stage = updates.get("stage_name")
        if raw_stage and str(raw_stage).strip():
            stage_name = assert_stage_name_unique(
                db,
                raw_stage,
                exclude_profile_id=profile.id,
            )
            updates["stage_name"] = stage_name
            # Keep profile slug aligned with username if defined; otherwise fallback to stage_name slug
            if new_username_val:
                updates["slug"] = new_username_val
            elif current_user.username:
                updates["slug"] = current_user.username
            else:
                updates["slug"] = next_available_musician_slug(
                    db, stage_name, exclude_profile_id=profile.id,
                )
        else:
            if profile.status == ProfileStatus.draft:
                updates["stage_name"] = None
                if not new_username_val and not current_user.username:
                    updates["slug"] = None
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El nombre de artista es obligatorio",
                )

    demoted = demote_published_profile_if_changed(profile, updates)
    if demoted:
        notify_profile_needs_resubmit(
            db,
            user=current_user,
            profile_role="musician",
            profile_id=str(profile.id),
        )

    videos = profile.videos or []
    showreel = getattr(profile, "showreel_video_url", None)
    if showreel and showreel not in videos:
        profile.showreel_video_url = None

    db.commit()
    db.refresh(profile)

    return profile


@router.post(
    "/musician/onboard",
    response_model=MusicianProfileOut,
    status_code=status.HTTP_200_OK,
)
def onboard_musician_profile(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.musician:
        raise HTTPException(status_code=403, detail="Solo los músicos pueden realizar el onboarding")

    profile = _get_musician_profile_or_404(db, current_user)
    
    if profile.status == ProfileStatus.published:
        return profile
        
    missing = []
    if not profile.stage_name:
        missing.append("stage_name")
    if not profile.location_city:
        missing.append("location_city")
    if not profile.genres or len(profile.genres) == 0:
        missing.append("genres")
        
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Completa tu perfil antes de publicar. Faltan: {', '.join(missing)}",
        )
        
    profile.status = ProfileStatus.published
    profile.published_at = datetime.utcnow()
    profile.rejection_reason = None
    
    db.commit()
    db.refresh(profile)
    return profile


@router.get(
    "/musicians/{musician_id_or_slug}",
    response_model=MusicianProfilePublicOut,
    status_code=status.HTTP_200_OK,
)
def get_public_musician_profile(
    musician_id_or_slug: str,
    db: Session = Depends(deps.get_db),
):
    query = _published_musician_query(db)
    try:
        lookup_id = UUID(musician_id_or_slug)
    except ValueError:
        profile = query.filter(MusicianProfile.slug == musician_id_or_slug).first()
    else:
        profile = query.filter(MusicianProfile.id == lookup_id).first()

    if not profile or not is_musician_listed_publicly(db, profile):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil no encontrado",
        )

    return _to_musician_public_out(db, profile)


# =========================
#      CONTRACTOR
# =========================

@router.get(
    "/contractors/{contractor_id}",
    response_model=ContractorProfilePublicOut,
    status_code=status.HTTP_200_OK,
)
def get_public_contractor_profile(
    contractor_id: UUID,
    db: Session = Depends(deps.get_db),
):
    profile = (
        db.query(ContractorProfile)
        .options(joinedload(ContractorProfile.user))
        .join(User)
        .filter(
            ContractorProfile.id == contractor_id,
            ContractorProfile.status == ProfileStatus.published,
            User.is_verified.is_(True),
        )
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil no encontrado",
        )

    rec_rows = (
        db.query(ContractorRecommendation, MusicianProfile)
        .join(
            MusicianProfile,
            MusicianProfile.id == ContractorRecommendation.musician_id,
        )
        .filter(ContractorRecommendation.contractor_id == profile.id)
        .order_by(ContractorRecommendation.created_at.desc())
        .limit(30)
        .all()
    )
    recommendations = [
        ContractorPublicRecommendationOut(
            id=rec.id,
            rating=rec.rating,
            comment=rec.comment,
            musician_name=musician.stage_name,
            created_at=rec.created_at,
        )
        for rec, musician in rec_rows
    ]

    return ContractorProfilePublicOut(
        id=profile.id,
        fullname=profile.user.fullname,
        bio=profile.bio,
        city=profile.city,
        rating_avg=float(profile.rating_avg) if profile.rating_avg is not None else None,
        rating_count=profile.rating_count,
        is_verified=profile.user.is_verified,
        recommendations=recommendations,
    )


@router.get(
    "/contractor/me",
    response_model=ContractorProfileOut,
    status_code=status.HTTP_200_OK,
)
def get_my_contractor_profile(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo los contratistas pueden acceder a su perfil",
        )
    return _get_contractor_profile_or_404(db, current_user)


@router.get(
    "/contractor/me/status",
    response_model=ProfileValidationOut,
    status_code=status.HTTP_200_OK,
)
def get_my_contractor_profile_status(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(status_code=403, detail="Solo los contratistas pueden acceder")
    profile = _get_contractor_profile_or_404(db, current_user)
    return _validation_out(validate_contractor_profile(profile))


@router.post(
    "/contractor/submit",
    response_model=ContractorProfileOut,
    status_code=status.HTTP_200_OK,
)
def submit_contractor_profile(
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(status_code=403, detail="Solo los contratistas pueden enviar su perfil")

    profile = _get_contractor_profile_or_404(db, current_user)
    validation = validate_contractor_profile(profile)
    if not validation.can_submit:
        missing = [field for step in validation.steps for field in step.missing]
        raise HTTPException(
            status_code=400,
            detail=f"Completa tu perfil antes de enviarlo. Faltan: {', '.join(missing)}",
        )

    profile.status = ProfileStatus.pending_review
    profile.submitted_at = datetime.utcnow()
    profile.rejection_reason = None
    notify_profile_submitted(
        db,
        user=current_user,
        profile_role="contractor",
        profile_id=str(profile.id),
        display_name=current_user.fullname,
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.post(
    "/contractor",
    response_model=ContractorProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_contractor_profile(
    payload: ContractorProfileCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo usuarios con rol 'contractor' pueden crear este perfil",
        )

    existing = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.user_id == current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El perfil de contratista ya existe. Debes actualizarlo.",
        )

    profile = ContractorProfile(
        user_id=current_user.id,
        status=ProfileStatus.draft,
        bio=payload.bio,
        preferences=payload.preferences,
        document_type=payload.document_type,
        document_number=assert_document_number_unique(db, payload.document_number),
        address=payload.address,
        city=payload.city,
        id_document_url=payload.id_document_url,
    )

    db.add(profile)
    db.commit()
    db.refresh(profile)

    return profile


@router.put(
    "/contractor",
    response_model=ContractorProfileOut,
    status_code=status.HTTP_200_OK,
)
def update_contractor_profile(
    payload: ContractorProfileUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    if current_user.role != UserRole.contractor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo usuarios con rol 'contractor' pueden editar este perfil",
        )

    profile = _get_contractor_profile_or_404(db, current_user)

    updates = payload.model_dump(exclude_unset=True)

    if "username" in updates:
        raw_username = updates.pop("username")
        if raw_username is not None and raw_username.strip():
            normalized_username = assert_username_unique(
                db,
                raw_username,
                exclude_user_id=current_user.id,
            )
            current_user.username = normalized_username
        elif raw_username is not None:
            current_user.username = None

    if "fullname" in updates:
        raw_fullname = updates.pop("fullname")
        if raw_fullname is not None:
            clean_fullname = raw_fullname.strip()
            if len(clean_fullname) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El nombre completo debe tener al menos 2 caracteres",
                )
            current_user.fullname = clean_fullname

    if "phone" in updates:
        raw_phone = updates.pop("phone")
        if raw_phone is not None and raw_phone.strip():
            current_user.phone = assert_phone_unique(
                db,
                raw_phone,
                exclude_user_id=current_user.id,
            )
        elif raw_phone is not None:
            current_user.phone = None

    if "document_number" in updates:
        updates["document_number"] = assert_document_number_unique(
            db,
            updates.get("document_number"),
            exclude_profile_id=profile.id,
        )

    demoted = demote_published_profile_if_changed(profile, updates)
    if demoted:
        notify_profile_needs_resubmit(
            db,
            user=current_user,
            profile_role="contractor",
            profile_id=str(profile.id),
        )

    db.commit()
    db.refresh(profile)

    return profile
