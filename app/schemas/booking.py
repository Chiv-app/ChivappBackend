from datetime import date, time, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from decimal import Decimal

from app.models.booking import BookingStatus


class BookingCreate(BaseModel):
    """Solicitud inicial del contratista (sin precio)."""
    musician_id: UUID
    event_date: date
    start_time: time
    end_time: time | None = None
    location_address: str
    location_city: str | None = None
    location_reference: str | None = None
    event_type: str
    event_description: str | None = None


class MusicianBookingCreate(BaseModel):
    """Reserva/contrata creada por el músico.

    Obligatorios: nombre del cliente, fecha, hora, tipo, ubicación y precio total.
    """

    contractor_fullname: str = Field(min_length=2, max_length=120)
    contractor_email: EmailStr | None = None
    contractor_phone: str | None = Field(default=None, max_length=40)
    document_type: str | None = Field(default=None, max_length=40)
    document_number: str | None = Field(default=None, max_length=60)
    contractor_address: str | None = Field(default=None, max_length=255)
    contractor_city: str | None = Field(default=None, max_length=120)

    event_date: date
    start_time: time
    end_time: time | None = None
    location_address: str = Field(min_length=3, max_length=255)
    location_city: str | None = Field(default=None, max_length=120)
    location_reference: str | None = Field(default=None, max_length=120)
    event_type: str = Field(min_length=2, max_length=120)
    event_description: str | None = Field(default=None, max_length=2000)

    price_agreed: Decimal = Field(gt=0)
    musician_quote_notes: str | None = Field(default=None, max_length=2000)


    @field_validator("contractor_email", mode="before")
    @classmethod
    def empty_email_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


class BookingUpdate(BaseModel):
    event_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location_address: str | None = None
    location_city: str | None = None
    location_reference: str | None = None
    event_type: str | None = None
    event_description: str | None = None


class BookingRequestedRepertoireUpdate(BaseModel):
    """Temas del repertorio que el contratista quiere para el show."""

    requested_repertoire: list[str] = Field(default_factory=list, max_length=80)

    @field_validator("requested_repertoire")
    @classmethod
    def clean_titles(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            title = (raw or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            cleaned.append(title[:160])
        return cleaned


class BookingReopenQuote(BaseModel):
    """Edita datos de la solicitud y vuelve al flujo de cotización."""

    event_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location_address: str | None = None
    location_city: str | None = None
    location_reference: str | None = None
    event_type: str | None = None
    event_description: str | None = None
    requested_repertoire: list[str] | None = None

    @field_validator("requested_repertoire")
    @classmethod
    def clean_titles(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            title = (raw or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            cleaned.append(title[:160])
        return cleaned


class BookingQuote(BaseModel):
    """Respuesta del músico con cotización y detalle solicitado."""
    price_agreed: Decimal = Field(gt=0)
    musician_quote_notes: str | None = None
    location_address: str | None = None
    location_city: str | None = None
    location_reference: str | None = None


class BookingReject(BaseModel):
    rejection_reason: str | None = None


class CancellationQuoteOut(BaseModel):
    """Vista previa del reembolso si se cancela ahora."""

    can_cancel: bool
    cancelled_by: str
    days_before_event: int
    paid_total: Decimal
    refundable_base: Decimal
    refund_percent: int
    refund_amount: Decimal
    rule: str


class BookingEventChangeRequest(BaseModel):
    location_address: str | None = None
    location_city: str | None = None
    location_reference: str | None = None
    event_description: str | None = None
    change_notes: str | None = None
    # Pago único (100%): tras el pago el precio no cambia; si se envía debe
    # coincidir con el actual.
    price_agreed: Decimal | None = Field(default=None, gt=0)


class BookingChangeDecision(BaseModel):
    """Aceptar o rechazar cambios pendientes (sin re-firma)."""
    accept: bool
    # Pago único (100%): si se envía, debe coincidir con el precio actual.
    price_agreed: Decimal | None = Field(default=None, gt=0)


class BookingMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class BookingMessageOut(BaseModel):
    id: UUID
    booking_id: UUID
    sender_user_id: UUID
    sender_name: str | None = None
    body: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    emoji: str | None = Field(default=None, max_length=16)
    comment: str | None = Field(default=None, max_length=2000)
    photo_urls: list[str] = Field(default_factory=list)
    video_urls: list[str] = Field(default_factory=list)
    is_final: bool = False


class BookingFinalReviewCreate(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    # If set, only the complaint is registered (no public review).
    complaint_reason: str | None = Field(default=None, max_length=2000)
    complaint_evidence_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_review_or_complaint(self):
        reason = (self.complaint_reason or "").strip()
        if reason:
            if len(reason) < 10:
                raise ValueError(
                    "La queja requiere al menos 10 caracteres."
                )
            return self

        comment = (self.comment or "").strip()
        if self.rating is None:
            raise ValueError("La reseña final requiere una calificación.")
        if len(comment) < 10:
            raise ValueError(
                "La reseña final requiere un comentario de al menos 10 caracteres."
            )
        self.comment = comment
        return self


class BookingGuestReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    emoji: str | None = Field(default=None, max_length=16)
    comment: str | None = Field(default=None, max_length=2000)
    photo_urls: list[str] = Field(default_factory=list)
    video_urls: list[str] = Field(default_factory=list)
    guest_name: str = Field(min_length=2, max_length=80)


class BookingReviewOut(BaseModel):
    id: UUID
    booking_id: UUID
    author_user_id: UUID | None = None
    guest_name: str | None = None
    author_label: str | None = None
    rating: int
    emoji: str | None
    comment: str | None
    photo_urls: list[str] | None
    video_urls: list[str] | None
    is_final: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContractorRecommendationCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(min_length=10, max_length=2000)


class ContractorRecommendationOut(BaseModel):
    id: UUID
    booking_id: UUID
    musician_id: UUID
    contractor_id: UUID
    rating: int
    comment: str
    musician_name: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingShareOut(BaseModel):
    booking_id: UUID
    enabled: bool
    can_enable: bool
    token: str | None = None
    path: str | None = None
    reason: str | None = None
    share_enabled_at: datetime | None = None


class BookingSharePublicOut(BaseModel):
    token: str
    event_type: str
    event_date: date
    start_time: time
    end_time: time | None = None
    location_city: str | None = None
    musician_name: str | None = None
    status: BookingStatus
    reactions_open: bool
    message: str


class BookingOut(BaseModel):
    id: UUID
    contractor_id: UUID
    musician_id: UUID
    event_date: date
    start_time: time
    end_time: time | None = None
    location_address: str
    location_city: str | None
    location_reference: str | None
    event_type: str
    event_description: str | None
    requested_repertoire: list[str] | None = None
    price_agreed: Decimal | None
    platform_fee_percent: Decimal | None = None
    platform_fee_amount: Decimal | None = None
    musician_quote_notes: str | None = None
    quoted_at: datetime | None = None
    rejection_reason: str | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancellation_refund_percent: Decimal | None = None
    cancellation_refund_amount: Decimal | None = None
    cancellation_refund_status: str | None = None
    pending_location_address: str | None = None
    pending_location_city: str | None = None
    pending_location_reference: str | None = None
    pending_event_description: str | None = None
    pending_change_notes: str | None = None
    pending_price_agreed: Decimal | None = None
    change_requested_by: str | None = None
    change_requested_at: datetime | None = None
    status: BookingStatus
    calendar_event_id: str | None = None
    created_at: datetime
    updated_at: datetime
    # Counterparty preview (optional; filled by serializers)
    musician_name: str | None = None
    musician_image_url: str | None = None
    contractor_name: str | None = None
    contractor_image_url: str | None = None
    # "owner" = lead musician / contractor; "member" = integrante asociado
    viewer_role: str | None = None
    # Estado de convocatoria del integrante (pending/accepted/declined)
    member_invite_status: str | None = None
    # Complaint summary for list cards / action filters
    complaint_status: str | None = None
    complaint_reason: str | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("requested_repertoire", mode="before")
    @classmethod
    def coerce_repertoire(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    @field_validator("id", "contractor_id", "musician_id", mode="before")
    @classmethod
    def coerce_uuid(cls, value):
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class BookingCalendarSyncRequest(BaseModel):
    include_contractor: bool = True
    ensemble_member_ids: list[UUID] = []



