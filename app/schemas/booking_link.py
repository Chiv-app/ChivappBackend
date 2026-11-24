from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.booking_link import BookingLinkStatus


class BookingLinkCreate(BaseModel):
    price: float = Field(..., ge=10.0, le=50000.0, description="Monto acordado en Soles (PEN)")
    event_date: Optional[date] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    event_type: Optional[str] = Field(default=None, max_length=120)
    location_address: Optional[str] = Field(default=None, max_length=300)
    location_city: Optional[str] = Field(default=None, max_length=120)
    location_reference: Optional[str] = Field(default=None, max_length=255)
    client_fullname: Optional[str] = Field(default=None, max_length=120)
    client_phone: Optional[str] = Field(default=None, max_length=40)
    client_email: Optional[EmailStr] = None
    client_document_number: Optional[str] = Field(default=None, max_length=20)
    event_description: Optional[str] = Field(default=None, max_length=2000)
    source: Optional[str] = Field(default="manual", max_length=32)

    @field_validator("client_email", mode="before")
    @classmethod
    def _empty_email_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("price")
    @classmethod
    def _validate_price_precision(cls, v: float) -> float:
        from decimal import Decimal

        d = Decimal(str(v))
        if d.as_tuple().exponent < -2:
            raise ValueError("El precio puede tener como máximo 2 decimales")
        return float(d)


class BookingLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    token: str
    musician_id: UUID
    price: float
    event_date: Optional[date] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    event_type: Optional[str] = None
    location_address: Optional[str] = None
    location_city: Optional[str] = None
    location_reference: Optional[str] = None
    client_fullname: Optional[str] = None
    client_phone: Optional[str] = None
    client_email: Optional[str] = None
    client_document_number: Optional[str] = None
    event_description: Optional[str] = None
    source: Optional[str] = "manual"
    status: BookingLinkStatus
    expires_at: datetime
    booking_id: Optional[UUID] = None
    open_count: int = 0
    first_opened_at: Optional[datetime] = None
    last_opened_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    current_step: int = 0
    max_step_reached: int = 0
    draft_data: Optional[Dict[str, Any]] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MusicianReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    guest_name: Optional[str] = "Cliente Verificado"
    rating: int
    comment: Optional[str] = None
    created_at: datetime


class MusicianLinkProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stage_name: str
    profile_image_url: Optional[str] = None
    portfolio_description: Optional[str] = None
    gallery_images: Optional[List[str]] = None
    videos: Optional[List[str]] = None
    showreel_video_url: Optional[str] = None
    rating_avg: Optional[float] = 0.0
    rating_count: Optional[int] = 0
    genres: Optional[List[str]] = None
    instruments: Optional[List[str]] = None
    location_city: Optional[str] = None
    reviews: Optional[List[MusicianReviewOut]] = []


class CancellationPolicyPublicOut(BaseModel):
    full_refund_min_days: int = 15
    partial_refund_min_days: int = 7
    partial_refund_percent: int = 50


class BookingLinkPublicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    price: float
    fee_percent: Optional[float] = 0.0
    fee_amount: Optional[float] = 0.0
    total_payable: Optional[float] = 0.0
    event_date: Optional[date] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    event_type: Optional[str] = None
    location_address: Optional[str] = None
    location_city: Optional[str] = None
    location_reference: Optional[str] = None
    client_fullname: Optional[str] = None
    client_phone: Optional[str] = None
    client_email: Optional[str] = None
    client_document_number: Optional[str] = None
    event_description: Optional[str] = None
    source: Optional[str] = "manual"
    musician: MusicianLinkProfile
    status: BookingLinkStatus
    expires_at: datetime
    cancellation_policy: CancellationPolicyPublicOut = Field(
        default_factory=CancellationPolicyPublicOut
    )
    contract_template_title: Optional[str] = None
    contract_template_body: Optional[str] = None
    open_count: int = 0
    first_opened_at: Optional[datetime] = None
    last_opened_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    current_step: int = 0
    max_step_reached: int = 0
    draft_data: Optional[Dict[str, Any]] = Field(default_factory=dict)


class BookingLinkProgressUpdate(BaseModel):
    record_open: bool = False
    current_step: Optional[int] = Field(default=None, ge=1, le=4)
    event_date: Optional[str] = Field(default=None, max_length=20)
    start_time: Optional[str] = Field(default=None, max_length=20)
    end_time: Optional[str] = Field(default=None, max_length=20)
    event_type: Optional[str] = Field(default=None, max_length=120)
    location_address: Optional[str] = Field(default=None, max_length=300)
    location_city: Optional[str] = Field(default=None, max_length=120)
    location_reference: Optional[str] = Field(default=None, max_length=255)
    event_description: Optional[str] = Field(default=None, max_length=2000)
    client_fullname: Optional[str] = Field(default=None, max_length=120)
    client_phone: Optional[str] = Field(default=None, max_length=40)
    client_email: Optional[str] = Field(default=None, max_length=255)
    client_document_number: Optional[str] = Field(default=None, max_length=20)
    has_signature: Optional[bool] = None
    payment_modal_opened: Optional[bool] = None


class BookingLinkProgressOut(BaseModel):
    open_count: int = 0
    first_opened_at: Optional[datetime] = None
    last_opened_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    current_step: int = 0
    max_step_reached: int = 0
    draft_data: Dict[str, Any] = Field(default_factory=dict)
    saved_at: datetime


class BookingLinkCheckout(BaseModel):
    event_date: date
    start_time: time
    end_time: Optional[time] = None
    event_type: str = Field(..., min_length=2, max_length=100)
    location_address: str = Field(..., min_length=5, max_length=300)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_reference: Optional[str] = Field(default=None, max_length=255)
    event_description: Optional[str] = Field(default=None, max_length=2000)

    # Customer fields
    fullname: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=6, max_length=30)
    document_number: Optional[str] = Field(None, min_length=8, max_length=20)
    signature_image_url: Optional[str] = None
    accept_terms: bool = True
    otp: Optional[str] = Field(default=None, min_length=6, max_length=6)
    password: Optional[str] = Field(default=None, max_length=128)

    @field_validator("accept_terms")
    @classmethod
    def _must_accept_terms(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Debes aceptar los términos y condiciones para continuar.")
        return value

    @field_validator("document_number", mode="before")
    @classmethod
    def _empty_document_number_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class BookingLinkIdentityCheck(BaseModel):
    email: EmailStr


class BookingLinkIdentityResponse(BaseModel):
    status: Literal["self", "exists_verified", "exists_unverified", "new"]
    message: Optional[str] = None


class BookingLinkOtpRequest(BaseModel):
    email: EmailStr


class BookingLinkAutocompleteRequest(BaseModel):
    text: Optional[str] = Field(default=None, max_length=4000)
    audio_base64: Optional[str] = Field(
        default=None, max_length=7_000_000, description="Audio en base64 o data URL (hasta ~5 MB)"
    )
    audio_mime_type: Optional[str] = Field(default="audio/webm", max_length=64)


class BookingLinkAutocompleteOut(BaseModel):
    transcript: Optional[str] = None
    price: Optional[float] = None
    event_date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    event_type: Optional[str] = None
    location_address: Optional[str] = None
    location_city: Optional[str] = None
    location_reference: Optional[str] = None
    client_fullname: Optional[str] = None
    client_phone: Optional[str] = None
    client_email: Optional[str] = None
    client_document_number: Optional[str] = None
    event_description: Optional[str] = None
