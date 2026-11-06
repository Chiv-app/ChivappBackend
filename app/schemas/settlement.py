from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.booking_complaint import BookingComplaintStatus


class PlatformPaymentInstructionsOut(BaseModel):
    phone_number: str = ""
    phone_label: str = "Yape / Plin"
    account_name: str | None = None
    qr_image_url: str | None = None
    instructions: str | None = None
    platform_fee_percent: float = 0
    gateway_fee_rate: float = 0.0412
    gateway_fixed_fee_pen: float = 1.18
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_validator("phone_number", "phone_label", mode="before")
    @classmethod
    def coerce_str(cls, value):
        return "" if value is None else value

    @field_validator("platform_fee_percent", mode="before")
    @classmethod
    def coerce_fee(cls, value):
        if value is None:
            return 0.0
        return float(value)

    @field_validator("gateway_fee_rate", mode="before")
    @classmethod
    def coerce_gateway_rate(cls, value):
        from app.core.config import settings

        if value is None:
            return float(settings.GATEWAY_FEE_RATE)
        return float(value)

    @field_validator("gateway_fixed_fee_pen", mode="before")
    @classmethod
    def coerce_gateway_fixed(cls, value):
        from app.core.config import settings

        if value is None:
            return float(settings.GATEWAY_FIXED_FEE_PEN)
        return float(value)


class PlatformPaymentInstructionsUpdate(BaseModel):
    phone_number: str = Field(min_length=6, max_length=40)
    phone_label: str = Field(default="Yape / Plin", min_length=1, max_length=80)
    account_name: str | None = Field(default=None, max_length=120)
    qr_image_url: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=1000)
    platform_fee_percent: float = Field(default=0, ge=0, le=100)

    @field_validator("phone_number", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        if value is None:
            return ""
        # Keep digits and leading +; strip spaces/dashes for storage consistency.
        raw = str(value).strip()
        return raw

    @field_validator("phone_label", mode="before")
    @classmethod
    def normalize_label(cls, value):
        text = ("" if value is None else str(value)).strip()
        return text or "Yape / Plin"

    @field_validator("qr_image_url", "account_name", "instructions", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None



class BookingComplaintCreate(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=500)


class BookingComplaintRespond(BaseModel):
    response: str = Field(min_length=10, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=500)


class BookingComplaintOut(BaseModel):
    id: UUID
    booking_id: UUID
    opened_by_user_id: UUID
    reason: str
    evidence_url: str | None = None
    status: BookingComplaintStatus
    musician_response: str | None = None
    musician_response_evidence_url: str | None = None
    musician_responded_at: datetime | None = None
    admin_musician_amount: float | None = None
    admin_contractor_refund: float | None = None
    admin_notes: str | None = None
    settled_at: datetime | None = None
    refund_status: str = "none"
    refund_evidence_url: str | None = None
    refund_sent_at: datetime | None = None
    refund_validated_at: datetime | None = None
    refund_rejection_reason: str | None = None
    refund_payment_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MusicianPayoutInfoOut(BaseModel):
    payout_method: str | None = None
    payout_bank_name: str | None = None
    payout_account_number: str | None = None
    payout_cci: str | None = None
    payout_phone: str | None = None
    payout_beneficiary_name: str | None = None
    payout_beneficiary_document: str | None = None
    payout_mp_email: str | None = None


class AdminSettlementOut(BaseModel):
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None = None
    price_agreed: float | None = None
    retained_total: float
    released_total: float
    retained_gross: float = 0
    released_gross: float = 0
    platform_fee_on_retained: float = 0
    currency: str = "PEN"
    musician_name: str | None = None
    musician_id: UUID | None = None
    contractor_name: str | None = None
    booking_status: str
    settlement_state: str
    complaint: BookingComplaintOut | None = None
    musician_payout_info: MusicianPayoutInfoOut | None = None
    payout_reference: str | None = None
    payout_evidence_url: str | None = None
    payout_notes: str | None = None


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



class AdminRefundTransfer(BaseModel):
    evidence_url: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class BookingRefundReject(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


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
    complaint_status: BookingComplaintStatus | None = None
    complaint_reason: str | None = None
    admin_musician_amount: float | None = None
    admin_contractor_refund: float | None = None


class MusicianEarningsSummaryV2(BaseModel):
    currency: str = "PEN"
    total_quoted: float
    total_app_debt: float
    total_disputed: float
    total_released: float
    total_retained_active: float
    total_pending: float
    total_retained: float
    bookings_active: int
    bookings_completed: int
    bookings_cancelled: int
    debts: list[MusicianDebtItem]
