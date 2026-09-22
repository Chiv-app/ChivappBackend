from datetime import datetime
from uuid import UUID
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.musician_profile import AvailabilityType
from app.models.profile_status import ProfileStatus


class PlatformStatsOut(BaseModel):
    musicians_count: int
    completed_bookings_count: int
    average_rating: float | None = None


class ValidationStepOut(BaseModel):
    id: int
    key: str
    label: str
    completed: bool
    missing: list[str] = Field(default_factory=list)
    required: bool = True


class ProfileValidationOut(BaseModel):
    status: ProfileStatus
    current_step: int
    steps: list[ValidationStepOut]
    can_submit: bool
    is_public: bool
    is_ensemble_only: bool = False


class ProfileReviewAction(BaseModel):
    rejection_reason: str | None = None


class RepertoireItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    youtube_url: str | None = None


# ---------- MUSICIAN PROFILE ----------

class MusicianProfileBase(BaseModel):
    is_ensemble_only: bool = False
    stage_name: str | None = None
    bio: str | None = None
    genres: list[str] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    songs: list[str] = Field(default_factory=list)
    repertoire: list[RepertoireItem] = Field(default_factory=list)
    price_per_hour: float | None = None
    price_per_event: float | None = None
    portfolio_description: str | None = None
    location_city: str | None = None
    location_zone: str | None = None
    availability_type: AvailabilityType | None = None
    profile_image_url: str | None = None
    gallery_images: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    showreel_video_url: str | None = None
    instagram_url: str | None = None
    facebook_url: str | None = None
    tiktok_url: str | None = None
    youtube_channel_url: str | None = None
    spotify_url: str | None = None
    website_url: str | None = None
    id_document_url: str | None = None
    contract_template_title: str | None = None
    contract_template_body: str | None = None
    contract_pdf_url: str | None = None
    signature_image_url: str | None = None
    # Payout fields
    payout_method: str | None = None
    payout_bank_name: str | None = None
    payout_account_number: str | None = None
    payout_cci: str | None = None
    payout_phone: str | None = None
    payout_beneficiary_name: str | None = None
    payout_beneficiary_document: str | None = None
    payout_mp_email: str | None = None


class MusicianProfileCreate(MusicianProfileBase):
    stage_name: str


class MusicianProfileUpdate(BaseModel):
    is_ensemble_only: bool | None = None
    stage_name: str | None = None
    username: str | None = None
    fullname: str | None = None
    bio: str | None = None
    genres: list[str] | None = None
    instruments: list[str] | None = None
    songs: list[str] | None = None
    repertoire: list[RepertoireItem] | None = None
    price_per_hour: float | None = None
    price_per_event: float | None = None
    portfolio_description: str | None = None
    location_city: str | None = None
    location_zone: str | None = None
    availability_type: AvailabilityType | None = None
    profile_image_url: str | None = None
    gallery_images: list[str] | None = None
    videos: list[str] | None = None
    showreel_video_url: str | None = None
    instagram_url: str | None = None
    facebook_url: str | None = None
    tiktok_url: str | None = None
    youtube_channel_url: str | None = None
    spotify_url: str | None = None
    website_url: str | None = None
    id_document_url: str | None = None
    contract_template_title: str | None = None
    contract_template_body: str | None = None
    signature_image_url: str | None = None
    payout_method: str | None = None
    payout_bank_name: str | None = None
    payout_account_number: str | None = None
    payout_cci: str | None = None
    payout_phone: str | None = None
    payout_beneficiary_name: str | None = None
    payout_beneficiary_document: str | None = None
    payout_mp_email: str | None = None



def _sync_songs_and_repertoire(
    songs: list[str],
    repertoire: list[RepertoireItem],
) -> tuple[list[str], list[RepertoireItem]]:
    if repertoire:
        titles = [item.title for item in repertoire]
        return titles, repertoire
    if songs:
        items = [RepertoireItem(title=title) for title in songs if title]
        return songs, items
    return [], []


class MusicianProfileOut(MusicianProfileBase):
    id: UUID
    user_id: UUID
    stage_name: str | None = None
    slug: str | None = None
    username: str | None = None
    fullname: str | None = None
    email: str | None = None
    status: ProfileStatus
    submitted_at: datetime | None = None
    published_at: datetime | None = None
    rejection_reason: str | None = None
    price_per_hour: Decimal | None = None
    price_per_event: Decimal | None = None
    rating_avg: float | None = None
    rating_count: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator(
        "genres",
        "instruments",
        "songs",
        "repertoire",
        "gallery_images",
        "videos",
        mode="before",
    )
    @classmethod
    def normalize_list_fields(cls, value):
        return value or []

    @model_validator(mode="after")
    def sync_repertoire(self):
        songs, repertoire = _sync_songs_and_repertoire(self.songs, self.repertoire)
        self.songs = songs
        self.repertoire = repertoire
        return self


class MusicianProfileListOut(BaseModel):
    id: UUID
    stage_name: str | None = None
    slug: str | None = None
    bio: str | None = None
    genres: list[str]
    instruments: list[str]
    songs: list[str]
    repertoire: list[RepertoireItem] = Field(default_factory=list)
    price_per_hour: float | None
    price_per_event: float | None
    availability_type: AvailabilityType | None = None
    location_city: str | None
    location_zone: str | None
    rating_avg: float | None
    rating_count: int | None
    profile_image_url: str | None
    gallery_images: list[str]
    videos: list[str]
    showreel_video_url: str | None = None
    is_verified: bool = True

    model_config = {"from_attributes": True}

    @field_validator(
        "genres",
        "instruments",
        "songs",
        "repertoire",
        "gallery_images",
        "videos",
        mode="before",
    )
    @classmethod
    def normalize_list_fields(cls, value):
        return value or []

    @model_validator(mode="after")
    def sync_repertoire(self):
        songs, repertoire = _sync_songs_and_repertoire(self.songs, self.repertoire)
        self.songs = songs
        self.repertoire = repertoire
        return self


class MusicianPublicReviewOut(BaseModel):
    id: UUID
    rating: int
    comment: str
    author_label: str
    event_type: str | None = None
    created_at: datetime


class MusicianProfilePublicOut(BaseModel):
    id: UUID
    stage_name: str | None = None
    slug: str | None = None
    bio: str | None
    genres: list[str]
    instruments: list[str]
    songs: list[str]
    repertoire: list[RepertoireItem] = Field(default_factory=list)
    price_per_hour: float | None
    price_per_event: float | None
    portfolio_description: str | None
    location_city: str | None
    location_zone: str | None
    availability_type: AvailabilityType | None
    rating_avg: float | None
    rating_count: int | None
    profile_image_url: str | None
    gallery_images: list[str]
    videos: list[str]
    showreel_video_url: str | None = None
    is_verified: bool = True
    instagram_url: str | None = None
    facebook_url: str | None = None
    tiktok_url: str | None = None
    youtube_channel_url: str | None = None
    spotify_url: str | None = None
    website_url: str | None = None
    reviews: list[MusicianPublicReviewOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    @field_validator(
        "genres",
        "instruments",
        "songs",
        "repertoire",
        "gallery_images",
        "videos",
        mode="before",
    )
    @classmethod
    def normalize_list_fields(cls, value):
        return value or []

    @model_validator(mode="after")
    def sync_repertoire(self):
        songs, repertoire = _sync_songs_and_repertoire(self.songs, self.repertoire)
        self.songs = songs
        self.repertoire = repertoire
        return self


class ContractorPublicRecommendationOut(BaseModel):
    id: UUID
    rating: int
    comment: str
    musician_name: str
    created_at: datetime


class ContractorProfilePublicOut(BaseModel):
    id: UUID
    fullname: str
    bio: str | None = None
    city: str | None = None
    rating_avg: float | None = None
    rating_count: int | None = None
    is_verified: bool = True
    recommendations: list[ContractorPublicRecommendationOut] = Field(
        default_factory=list
    )


class MusicianProfileAdminOut(MusicianProfileOut):
    user_email: str | None = None
    user_fullname: str | None = None
    user_phone: str | None = None


class MusicianContractGenerateOut(BaseModel):
    contract_pdf_url: str


class ContractorContractGenerateOut(BaseModel):
    contract_pdf_url: str


# ---------- CONTRACTOR PROFILE ----------

class ContractorProfileBase(BaseModel):
    bio: str | None = None
    preferences: list[str] = Field(default_factory=list)
    document_type: str | None = None
    document_number: str | None = None
    address: str | None = None
    city: str | None = None
    id_document_url: str | None = None
    contract_template_title: str | None = None
    contract_template_body: str | None = None
    contract_pdf_url: str | None = None


class ContractorProfileCreate(ContractorProfileBase):
    pass


class ContractorProfileUpdate(BaseModel):
    username: str | None = None
    fullname: str | None = None
    phone: str | None = None
    bio: str | None = None
    preferences: list[str] | None = None
    document_type: str | None = None
    document_number: str | None = None
    address: str | None = None
    city: str | None = None
    id_document_url: str | None = None
    contract_template_title: str | None = None
    contract_template_body: str | None = None


class ContractorProfileOut(ContractorProfileBase):
    id: UUID
    user_id: UUID
    username: str | None = None
    fullname: str | None = None
    email: str | None = None
    phone: str | None = None
    status: ProfileStatus
    submitted_at: datetime | None = None
    published_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("preferences", mode="before")
    @classmethod
    def normalize_preferences(cls, value):
        return value or []


class ContractorProfileAdminOut(ContractorProfileOut):
    user_email: str | None = None
    user_fullname: str | None = None
    user_phone: str | None = None
