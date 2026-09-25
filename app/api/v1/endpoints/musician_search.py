from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.api import deps
from app.models.musician_profile import MusicianProfile
from app.models.musician_media import MusicianMedia, MediaType
from app.models.profile_status import ProfileStatus
from app.models.user import User
from app.schemas.musician_search import (
    MusicianSearchFilters,
    MusicianSearchResult,
)
from app.services.profile_visibility import is_musician_listed_publicly

router = APIRouter(prefix="/musicians", tags=["Musician Search"])


@router.post("/search", response_model=list[MusicianSearchResult])
def search_musicians(
    filters: MusicianSearchFilters,
    db: Session = Depends(deps.get_db),
):
    query = (
        db.query(MusicianProfile)
        .join(User)
        .filter(
            MusicianProfile.status == ProfileStatus.published,
            User.is_verified.is_(True),
        )
        .order_by(
            MusicianProfile.rating_avg.desc().nulls_last(),
            MusicianProfile.created_at.desc(),
        )
    )

    if filters.city:
        query = query.filter(MusicianProfile.location_city.ilike(f"%{filters.city}%"))

    if filters.genres:
        from sqlalchemy import or_, func
        conditions = [func.array_to_string(MusicianProfile.genres, ',').ilike(f"%{g}%") for g in filters.genres]
        query = query.filter(or_(*conditions))

    if filters.instruments:
        from sqlalchemy import or_, func
        conditions = [func.array_to_string(MusicianProfile.instruments, ',').ilike(f"%{i}%") for i in filters.instruments]
        query = query.filter(or_(*conditions))

    if filters.min_price_per_event is not None:
        query = query.filter(MusicianProfile.price_per_event >= filters.min_price_per_event)

    if filters.max_price_per_event is not None:
        query = query.filter(MusicianProfile.price_per_event <= filters.max_price_per_event)

    musicians = query.all()
    results: list[MusicianSearchResult] = []

    for m in musicians:
        if not is_musician_listed_publicly(db, m):
            continue

        main_image = (
            db.query(MusicianMedia)
            .filter(
                MusicianMedia.musician_id == m.id,
                MusicianMedia.type == MediaType.image
            )
            .order_by(MusicianMedia.order_index.asc())
            .first()
        )

        results.append(
            MusicianSearchResult(
                musician_id=str(m.id),
                user_id=str(m.user_id),
                slug=getattr(m, "slug", None),
                stage_name=m.stage_name,
                city=m.location_city,
                zone=m.location_zone,
                genres=m.genres or [],
                instruments=m.instruments or [],
                price_per_event=float(m.price_per_event) if m.price_per_event is not None else None,
                rating_avg=float(m.rating_avg) if m.rating_avg is not None else None,
                rating_count=int(m.rating_count) if m.rating_count is not None else None,
                main_image_url=main_image.url if main_image else None,
                is_verified=m.user.is_verified,
            )
        )

    # skip/limit se aplican después del filtro de visibilidad (igual que
    # /profiles/musicians): no todos los candidatos de la query SQL terminan
    # siendo listables públicamente.
    skip = max(filters.skip, 0)
    limit = max(min(filters.limit, 50), 1)
    return results[skip : skip + limit]
