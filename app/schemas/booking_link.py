from datetime import date, datetime, time
from typing import List, Optional, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.booking_link import BookingLinkStatus


class BookingLinkCreate(BaseModel):
    price: float = Field(..., ge=10.0, le=50000.0, description="Monto acordado en Soles (PEN)")
    event_date: Optional[date] = None
    start_time: Optional[time] = None
    client_email: Optional[EmailStr] = None

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
    client_email: Optional[str] = None
    status: BookingLinkStatus
    expires_at: datetime
    booking_id: Optional[UUID] = None
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
    client_email: Optional[str] = None
    musician: MusicianLinkProfile
    status: BookingLinkStatus
    expires_at: datetime
    cancellation_policy: CancellationPolicyPublicOut = Field(
        default_factory=CancellationPolicyPublicOut
    )
    contract_template_title: Optional[str] = None
    contract_template_body: Optional[str] = None


class BookingLinkCheckout(BaseModel):
    event_date: date
    start_time: time
    event_type: str = Field(..., min_length=2, max_length=100)
    location_address: str = Field(..., min_length=5, max_length=300)
    location_city: Optional[str] = Field(default=None, max_length=100)
    location_reference: Optional[str] = Field(default=None, max_length=255)

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
