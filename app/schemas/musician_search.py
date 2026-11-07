from datetime import date
from pydantic import BaseModel, Field
from typing import Annotated, List

# Topes para que una sola búsqueda no arme cientos de condiciones LIKE sobre toda la tabla (S-09).
SearchTerm = Annotated[str, Field(max_length=60)]


class MusicianSearchFilters(BaseModel):
    city: str | None = Field(default=None, max_length=120)
    genres: List[SearchTerm] | None = Field(default=None, max_length=15)
    instruments: List[SearchTerm] | None = Field(default=None, max_length=15)
    min_price_per_event: float | None = None
    max_price_per_event: float | None = None
    available_date: date | None = None
    skip: int = 0
    limit: int = 20


class MusicianSearchResult(BaseModel):
    musician_id: str           # musician_profile.id
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
