from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.api import deps
from app.core.timezone import now_peru
from app.models.musician_availability import MusicianAvailability
from app.models.musician_profile import MusicianProfile
from app.models.musician_media import MusicianMedia, MediaType
from app.models.profile_status import ProfileStatus
from app.models.user import User
from app.schemas.musician_search import (
    MusicianSearchFilters,
    MusicianSearchResult,
)
from app.services.availability_match import matching_slots
from app.services.profile_visibility import (
    is_musician_listed_publicly,
    musician_listed_sql_filters,
)

router = APIRouter(prefix="/musicians", tags=["Musician Search"])

# Antelación mínima que pide cada opción de details.notice, en días.
NOTICE_DAYS = {"0": 0, "2d": 2, "1w": 7, "2w": 14, "1m": 30}

# Con filtro por fecha hay que cruzar horarios en Python; se acota cuántos candidatos se revisan.
MAX_DATE_FILTER_SCAN = 500


def _escape_like(val: str) -> str:
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.post("/search", response_model=list[MusicianSearchResult])
def search_musicians(
    filters: MusicianSearchFilters,
    db: Session = Depends(deps.get_db),
):
    query = (
        db.query(MusicianProfile)
        .join(User)
        .options(joinedload(MusicianProfile.user))
        .filter(
            MusicianProfile.status == ProfileStatus.published,
            MusicianProfile.is_ensemble_only.is_(False),
            User.is_verified.is_(True),
            User.is_active.is_(True),
        )
        .order_by(
            MusicianProfile.rating_avg.desc().nulls_last(),
            MusicianProfile.created_at.desc(),
            MusicianProfile.id.asc(),
        )
    )

    if filters.city:
        from sqlalchemy import or_

        escaped_city = _escape_like(filters.city.strip())
        # También aparece quien declaró esa ciudad como otra donde se presenta (details.other_cities).
        query = query.filter(
            or_(
                MusicianProfile.location_city.ilike(f"%{escaped_city}%", escape="\\"),
                MusicianProfile.details["other_cities"].astext.ilike(f'%"{escaped_city}"%', escape="\\"),
            )
        )

    if filters.profile_type:
        query = query.filter(MusicianProfile.profile_type == filters.profile_type)

    if filters.group_sizes:
        query = query.filter(MusicianProfile.group_size.in_(filters.group_sizes))

    for event_type in dict.fromkeys(filters.event_types or []):
        query = query.filter(MusicianProfile.details.contains({"event_types": [event_type]}))

    if filters.genres:
        from sqlalchemy import or_, func
        conditions = [
            func.array_to_string(MusicianProfile.genres, ',').ilike(f"%{_escape_like(g.strip())}%", escape="\\")
            for g in filters.genres
        ]
        query = query.filter(or_(*conditions))

    if filters.instruments:
        from sqlalchemy import or_, func
        conditions = [
            func.array_to_string(MusicianProfile.instruments, ',').ilike(f"%{_escape_like(i.strip())}%", escape="\\")
            for i in filters.instruments
        ]
        query = query.filter(or_(*conditions))

    if filters.min_price_per_event is not None:
        query = query.filter(MusicianProfile.price_per_event >= filters.min_price_per_event)

    if filters.max_price_per_event is not None:
        query = query.filter(MusicianProfile.price_per_event <= filters.max_price_per_event)

    skip = max(filters.skip, 0)
    limit = max(min(filters.limit, 50), 1)
    query = query.filter(*musician_listed_sql_filters())
    if filters.available_date is None:
        # Sin filtro de fecha la página se corta en la base de datos (no se carga todo el catálogo).
        query = query.offset(skip).limit(limit)
    else:
        query = query.limit(MAX_DATE_FILTER_SCAN)

    musicians = [m for m in query.all() if is_musician_listed_publicly(db, m)]
    if not musicians:
        return []

    musician_ids = [m.id for m in musicians]

    # WP-12: Batch load slots when filtering by available_date (evita N+1).
    slots_by_musician: dict = {}
    if filters.available_date is not None:
        slot_rows = (
            db.query(MusicianAvailability)
            .filter(MusicianAvailability.musician_id.in_(musician_ids))
            .order_by(MusicianAvailability.day_of_week.asc(), MusicianAvailability.start_time.asc())
            .all()
        )
        for s_row in slot_rows:
            slots_by_musician.setdefault(s_row.musician_id, []).append(s_row)

    # WP-12: Batch load main image per musician in a single query (evita N+1).
    main_image_by_musician: dict = {}
    media_rows = (
        db.query(MusicianMedia)
        .filter(
            MusicianMedia.musician_id.in_(musician_ids),
            MusicianMedia.type == MediaType.image,
        )
        .order_by(MusicianMedia.musician_id, MusicianMedia.order_index.asc())
        .all()
    )
    for media in media_rows:
        if media.musician_id not in main_image_by_musician:
            main_image_by_musician[media.musician_id] = media.url

    results: list[MusicianSearchResult] = []
    days_until_event = (filters.available_date - now_peru().date()).days if filters.available_date is not None else None
    for m in musicians:
        if filters.available_date is not None:
            slots = slots_by_musician.get(m.id, [])
            if slots and not matching_slots(slots, filters.available_date):
                continue
            # Quien pide más antelación de la que queda hasta el evento no puede atenderlo.
            notice_days = NOTICE_DAYS.get((m.details or {}).get("notice", ""), 0)
            if days_until_event is not None and notice_days > days_until_event:
                continue

        results.append(
            MusicianSearchResult(
                musician_id=str(m.id),
                slug=getattr(m, "slug", None),
                stage_name=m.stage_name,
                city=m.location_city,
                zone=m.location_zone,
                genres=m.genres or [],
                instruments=m.instruments or [],
                price_per_event=float(m.price_per_event) if m.price_per_event is not None else None,
                rating_avg=float(m.rating_avg) if m.rating_avg is not None else None,
                rating_count=int(m.rating_count) if m.rating_count is not None else None,
                main_image_url=main_image_by_musician.get(m.id),
                is_verified=m.user.is_verified,
                profile_type=m.profile_type or "solo",
                group_size=m.group_size,
            )
        )

    if filters.available_date is None:
        return results
    return results[skip : skip + limit]
