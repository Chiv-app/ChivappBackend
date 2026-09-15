from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.schemas.booking import BookingMessageOut, BookingReviewOut
from app.schemas.contract import ContractOut
from app.schemas.payment import PaymentOut
from app.schemas.settlement import BookingComplaintOut


class AdminStatsOut(BaseModel):
    total_users: int
    total_musicians: int
    total_contractors: int
    pending_musician_profiles: int
    pending_contractor_profiles: int
    published_musicians: int
    published_contractors: int
    total_bookings: int = 0
    active_bookings: int = 0
    change_pending_bookings: int = 0
    payment_review_bookings: int = 0
    completed_bookings: int = 0
    cancelled_bookings: int = 0
    total_payments: int = 0
    retained_payments_amount: float = 0
    released_payments_amount: float = 0
    share_enabled_bookings: int = 0


class AdminUserOut(BaseModel):
    id: UUID
    email: EmailStr
    fullname: str | None = None
    role: str
    phone: str | None
    is_verified: bool
    is_active: bool = True
    created_at: datetime
    last_login_at: datetime | None
    profile_picture_url: str | None = None

    model_config = {"from_attributes": True}


class AdminUserUpdate(BaseModel):
    is_verified: bool | None = None
    is_active: bool | None = None


class AdminActivityItem(BaseModel):
    id: str
    type: str
    title: str
    subtitle: str | None = None
    href: str | None = None
    created_at: datetime


class AdminBookingOut(BaseModel):
    id: UUID
    status: str
    event_type: str
    event_date: date
    start_time: time
    location_address: str
    location_city: str | None
    price_agreed: Decimal | None
    advance_amount: Decimal | None
    share_enabled: bool
    change_requested_by: str | None
    musician_id: UUID
    contractor_id: UUID
    musician_name: str | None = None
    contractor_name: str | None = None
    musician_email: str | None = None
    contractor_email: str | None = None
    cancelled_by: str | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminBookingDetailOut(AdminBookingOut):
    """Vista completa de una reserva para el admin: contrato, pagos, mensajes, queja/reseña."""

    musician_phone: str | None = None
    contractor_phone: str | None = None
    balance_due: float = 0
    amount_paid: float = 0
    contract: ContractOut | None = None
    payments: list[PaymentOut] = Field(default_factory=list)
    messages: list[BookingMessageOut] = Field(default_factory=list)
    complaint: BookingComplaintOut | None = None
    reviews: list[BookingReviewOut] = Field(default_factory=list)


class AdminBookingCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AdminPaymentOut(BaseModel):
    id: UUID
    booking_id: UUID
    amount: Decimal
    currency: str
    payment_type: str | None
    evidence_url: str | None
    evidence_urls: list[str] | None = None
    status: str
    retained_at: datetime | None
    released_at: datetime | None
    reviewed_by_user_id: UUID | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    event_type: str | None = None
    event_date: date | None = None
    musician_name: str | None = None
    contractor_name: str | None = None


class AdminPaymentReviewItem(BaseModel):
    """Item de la cola de comprobantes pendientes de validar/rechazar."""

    booking_id: UUID
    booking_status: str
    kind: str  # "advance" | "balance"
    payment_id: UUID
    amount: Decimal
    currency: str
    evidence_urls: list[str]
    event_type: str
    event_date: date
    musician_name: str | None = None
    contractor_name: str | None = None
    submitted_at: datetime
    previous_rejections: int = 0


class AdminPaymentReject(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AdminReleaseSettlement(BaseModel):
    payout_reference: str | None = Field(default=None, max_length=100)
    payout_evidence_url: str | None = Field(default=None, max_length=500)
    payout_notes: str | None = Field(default=None, max_length=2000)


class AdminSettleBooking(BaseModel):
    musician_amount: float = Field(ge=0)
    contractor_refund: float = Field(ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    payout_reference: str | None = Field(default=None, max_length=100)
    payout_evidence_url: str | None = Field(default=None, max_length=500)



class AdminProfileStatusUpdate(BaseModel):
    """Acciones de moderación sobre perfiles publicados/rechazados."""
    action: str = Field(pattern="^(unpublish|request_resubmit)$")
    reason: str | None = Field(default=None, max_length=1000)
