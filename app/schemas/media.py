from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator
from app.models.musician_media import MediaType
from app.schemas.profiles import validate_safe_public_url


class MusicianMediaBase(BaseModel):
    type: MediaType
    url: str
    thumbnail_url: str | None = None
    order_index: int = 0


class MusicianMediaCreate(MusicianMediaBase):
    @field_validator("url", "thumbnail_url", mode="before")
    @classmethod
    def _validate_urls(cls, value):
        return validate_safe_public_url(value)


class MusicianMediaUpdate(BaseModel):
    type: MediaType | None = None
    url: str | None = None
    thumbnail_url: str | None = None
    order_index: int | None = None

    @field_validator("url", "thumbnail_url", mode="before")
    @classmethod
    def _validate_urls(cls, value):
        return validate_safe_public_url(value)


class MusicianMediaOut(MusicianMediaBase):
    id: UUID
    musician_id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}
