from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class EnsembleMemberCreate(BaseModel):
    fullname: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = None
    specialties: list[str] = Field(default_factory=list, min_length=1)
    notes: str | None = None

    @field_validator("phone", "notes", mode="before")
    @classmethod
    def strip_optional_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("fullname")
    @classmethod
    def require_fullname(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("El nombre es obligatorio")
        return cleaned

    @field_validator("specialties")
    @classmethod
    def clean_specialties(cls, value: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        for item in value or []:
            label = (item or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(label)
        if not cleaned:
            raise ValueError("Agrega al menos una especialidad")
        return cleaned


class EnsembleMemberUpdate(BaseModel):
    fullname: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = None
    specialties: list[str] | None = None
    notes: str | None = None
    status: str | None = None

    @field_validator("fullname", "phone", "notes", mode="before")
    @classmethod
    def strip_optional(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("specialties")
    @classmethod
    def clean_specialties(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        seen = set()
        for item in value:
            label = (item or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(label)
        if not cleaned:
            raise ValueError("Agrega al menos una especialidad")
        return cleaned


class EnsembleMemberOut(BaseModel):
    id: UUID
    email: EmailStr
    fullname: str
    phone: str | None
    specialties: list[str]
    notes: str | None
    status: str
    has_password: bool
    invite_url: str | None = None
    invite_expires_at: datetime | None = None
    invite_expired: bool = False
    member_user_id: UUID | None
    invited_at: datetime | None
    joined_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingMemberInviteCreate(BaseModel):
    ensemble_member_ids: list[UUID] = Field(min_length=1)


class BookingMemberInviteOut(BaseModel):
    id: UUID
    booking_id: UUID
    ensemble_member_id: UUID
    member_fullname: str
    member_email: EmailStr
    specialties: list[str]
    status: str
    respond_url: str | None = None
    invited_at: datetime | None
    responded_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class BookingMemberInvitePreviewOut(BaseModel):
    event_type: str
    event_date: date
    start_time: time
    location_city: str | None
    location_address: str | None
    leader_name: str
    member_fullname: str
    status: str
    can_respond: bool
    needs_password: bool = False


class BookingMemberRespondRequest(BaseModel):
    action: str = Field(pattern="^(accept|decline)$")
    password: str | None = Field(default=None, min_length=8)


class BookingMemberPayoutItem(BaseModel):
    ensemble_member_id: UUID
    amount: Decimal = Field(ge=0)
    note: str | None = None


class BookingMemberPayoutUpdate(BaseModel):
    items: list[BookingMemberPayoutItem] = Field(default_factory=list)
    lock: bool = False


class BookingMemberPayoutOut(BaseModel):
    id: UUID
    booking_id: UUID
    ensemble_member_id: UUID
    member_fullname: str
    member_email: EmailStr
    amount: Decimal
    currency: str
    status: str
    note: str | None
    set_by_leader_at: datetime | None
    paid_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class MemberSettlementLineOut(BaseModel):
    ensemble_member_id: UUID
    member_fullname: str
    member_email: EmailStr
    invite_status: str
    payout_id: UUID | None = None
    amount: Decimal = Decimal("0")
    currency: str = "PEN"
    payout_status: str | None = None
    paid_at: datetime | None = None


class MemberSettlementBookingOut(BaseModel):
    booking_id: UUID
    event_type: str
    event_date: date
    start_time: time
    location_city: str | None
    status: str
    price_agreed: Decimal | None
    currency: str = "PEN"
    can_pay: bool
    has_contractor_review: bool
    members: list[MemberSettlementLineOut]
    total_assigned: Decimal
    total_paid: Decimal
    total_pending: Decimal


class MyMemberIncomeItem(BaseModel):
    payout_id: UUID
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None
    booking_status: str
    leader_name: str | None = None
    amount: Decimal
    currency: str = "PEN"
    status: str
    note: str | None = None
    set_by_leader_at: datetime | None = None
    paid_at: datetime | None = None


class MyMemberIncomeSummary(BaseModel):
    currency: str = "PEN"
    total_assigned: Decimal
    total_pending: Decimal
    total_paid: Decimal
    items: list[MyMemberIncomeItem]


class MemberPayoutHistoryItem(BaseModel):
    payout_id: UUID
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None
    booking_status: str
    amount: Decimal
    currency: str = "PEN"
    status: str
    note: str | None = None
    set_by_leader_at: datetime | None = None
    paid_at: datetime | None = None


class MemberPayoutHistoryOut(BaseModel):
    member_id: UUID
    member_fullname: str
    member_email: EmailStr
    currency: str = "PEN"
    total_assigned: Decimal
    total_pending: Decimal
    total_paid: Decimal
    shows_count: int
    items: list[MemberPayoutHistoryItem]


class MusicianReportSeriesPoint(BaseModel):
    bucket: str
    label: str
    bookings: int = 0
    revenue: float = 0
    released: float = 0
    retained: float = 0
    member_paid: float = 0


class MusicianReportStatusSlice(BaseModel):
    key: str
    label: str
    count: int
    value: float = 0


class MusicianReportMemberRow(BaseModel):
    ensemble_member_id: UUID
    fullname: str
    email: str
    status: str
    shows: int
    assigned: float
    paid: float
    pending: float


class MusicianReportBookingRow(BaseModel):
    booking_id: UUID
    event_type: str
    event_date: date
    location_city: str | None
    status: str
    price_agreed: float | None
    retained: float
    released: float
    member_assigned: float
    member_paid: float


class MusicianReportsOut(BaseModel):
    currency: str = "PEN"
    period_from: date
    period_to: date
    group_by: str
    bookings_total: int
    bookings_completed: int
    bookings_cancelled: int
    bookings_active: int
    revenue_quoted: float
    revenue_retained: float
    revenue_released: float
    member_assigned: float
    member_paid: float
    member_pending: float
    rating_avg: float | None
    rating_count: int
    complaints_total: int
    complaints_open: int
    complaints_settled: int
    series: list[MusicianReportSeriesPoint]
    bookings_by_status: list[MusicianReportStatusSlice]
    payouts_by_status: list[MusicianReportStatusSlice]
    top_members: list[MusicianReportMemberRow]
    bookings_table: list[MusicianReportBookingRow]
    members_table: list[MusicianReportMemberRow]

