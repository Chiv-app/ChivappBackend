from datetime import date, datetime
from uuid import UUID
from typing import Any

from pydantic import BaseModel, Field, model_validator
from app.models.payment import PaymentStatus


class PaymentUpdateStatus(BaseModel):
    status: PaymentStatus


class PaymentOut(BaseModel):
    id: UUID
    booking_id: UUID
    amount: float
    currency: str
    payment_type: str | None
    evidence_url: str | None
    evidence_urls: list[str] = Field(default_factory=list)
    status: PaymentStatus
    retained_at: datetime | None
    released_at: datetime | None
    reviewed_by_user_id: UUID | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None
    gateway_provider: str | None = None
    gateway_payment_id: str | None = None
    gateway_preference_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def coalesce_evidence(cls, data: Any) -> Any:
        if isinstance(data, dict):
            urls = [u for u in (data.get("evidence_urls") or []) if u]
            primary = data.get("evidence_url")
            if primary and primary not in urls:
                urls.insert(0, primary)
            return {
                **data,
                "evidence_urls": urls,
                "evidence_url": urls[0] if urls else None,
            }

        urls: list[str] = []
        for item in getattr(data, "evidence_urls", None) or []:
            if item and item not in urls:
                urls.append(item)
        primary = getattr(data, "evidence_url", None)
        if primary and primary not in urls:
            urls.insert(0, primary)

        amount = getattr(data, "amount", None)
        return {
            "id": data.id,
            "booking_id": data.booking_id,
            "amount": float(amount) if amount is not None else 0.0,
            "currency": data.currency,
            "payment_type": data.payment_type,
            "evidence_url": urls[0] if urls else None,
            "evidence_urls": urls,
            "status": data.status,
            "retained_at": data.retained_at,
            "released_at": data.released_at,
            "reviewed_by_user_id": getattr(data, "reviewed_by_user_id", None),
            "reviewed_at": getattr(data, "reviewed_at", None),
            "rejection_reason": getattr(data, "rejection_reason", None),
            "gateway_provider": getattr(data, "gateway_provider", None),
            "gateway_payment_id": getattr(data, "gateway_payment_id", None),
            "gateway_preference_id": getattr(data, "gateway_preference_id", None),
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


class MercadoPagoPreferenceRequest(BaseModel):
    booking_id: str
    payment_type: str = Field(default="full", pattern="^full$")
    amount: float | None = None
    signature_image_url: str | None = None
    sign_ip: str | None = None
    payer_email: str | None = None


class MercadoPagoPreferenceResponse(BaseModel):
    preference_id: str
    init_point: str
    sandbox_init_point: str | None = None
    public_key: str
    amount: float
    currency: str = "PEN"
    payment_type: str


class MercadoPagoPaymentCheckResponse(BaseModel):
    status: str
    payment_id: str | None = None
    booking_status: str
    is_approved: bool
    message: str


class MercadoPagoProcessPaymentRequest(BaseModel):
    booking_id: str
    payment_type: str = Field(default="full", pattern="^full$")
    token: str
    payment_method_id: str = "yape"
    amount: float | None = None
    installments: int = 1
    issuer_id: str | None = None
    payer_email: str | None = None
    device_id: str | None = None
    identification_type: str | None = None
    identification_number: str | None = None
    signature_image_url: str | None = None
    sign_ip: str | None = None


class MercadoPagoProcessPaymentResponse(BaseModel):
    success: bool
    status: str
    status_detail: str | None = None
    payment_id: str | None = None
    message: str


class MusicianEarningsItem(BaseModel):
    payment_id: UUID
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None = None
    booking_status: str
    amount: float
    currency: str
    payment_type: str | None
    status: PaymentStatus
    retained_at: datetime | None
    released_at: datetime | None
    created_at: datetime


class MusicianDebtItem(BaseModel):
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None = None
    price_agreed: float | None = None
    retained_total: float
    released_total: float
    currency: str = "PEN"
    booking_status: str
    debt_state: str
    complaint_status: str | None = None
    complaint_reason: str | None = None
    admin_musician_amount: float | None = None
    admin_contractor_refund: float | None = None


class MusicianEarningsSummary(BaseModel):
    currency: str = "PEN"
    total_quoted: float
    total_released: float
    total_retained: float
    total_pending: float
    total_app_debt: float = 0
    total_disputed: float = 0
    total_retained_active: float = 0
    bookings_active: int
    bookings_completed: int
    bookings_cancelled: int
    items: list[MusicianEarningsItem]
    debts: list[MusicianDebtItem] = []


class ContractorExpensesItem(BaseModel):
    payment_id: UUID
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None = None
    musician_stage_name: str | None = None
    booking_status: str
    amount: float
    currency: str
    payment_type: str | None
    status: PaymentStatus
    retained_at: datetime | None
    released_at: datetime | None
    created_at: datetime


class ContractorExpensesSummary(BaseModel):
    currency: str = "PEN"
    total_quoted: float
    total_released: float
    total_retained: float
    total_pending: float
    bookings_active: int
    bookings_completed: int
    bookings_cancelled: int
    items: list[ContractorExpensesItem]


class ContractorOperationItem(BaseModel):
    id: str
    booking_id: UUID
    kind: str
    status: str
    direction: str
    title: str
    subtitle: str | None = None
    cta_label: str
    amount: float | None = None
    currency: str = "PEN"
    event_type: str
    event_date: date
    location_city: str | None = None
    musician_stage_name: str | None = None
    booking_status: str
    payment_type: str | None = None
    payment_status: PaymentStatus | None = None
    complaint_status: str | None = None
    occurred_at: datetime
    source: str


class ContractorOperationsSummary(BaseModel):
    currency: str = "PEN"
    total_out: float
    total_in: float
    net_out: float
    total_quoted: float
    total_service: float = 0
    total_fees: float = 0
    total_released: float
    total_retained: float
    total_pending: float
    total_refund_pending: float = 0
    pending_me_count: int
    pending_other_count: int
    dispute_count: int
    bookings_active: int
    bookings_completed: int
    bookings_cancelled: int
    items: list[ContractorOperationItem]
