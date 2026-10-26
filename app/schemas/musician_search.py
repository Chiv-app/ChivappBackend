from datetime import date
from pydantic import BaseModel
from typing import List


class MusicianSearchFilters(BaseModel):
    city: str | None = None
    genres: List[str] | None = None
    instruments: List[str] | None = None
    min_price_per_event: float | None = None
    max_price_per_event: float | None = None
    available_date: date | None = None
    skip: int = 0
    limit: int = 20


class MusicianSearchResult(BaseModel):
    musician_id: str           # musician_profile.id
    user_id: str
    slug: str | None = None
    stage_name: str
    city: str | None
    zone: str | None
    genres: list[str] | None
    instruments: list[str] | None
    price_per_event: float | None
    rating_avg: float | None
    rating_count: int | None
    main_image_url: str | None = None
    is_verified: bool = True

    model_config = {"from_attributes": False}
