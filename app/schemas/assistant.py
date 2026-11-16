from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AssistantPackageItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=2, max_length=120)
    members_count: int = Field(default=1, ge=1, le=60)
    duration_hours: float = Field(default=1.0, gt=0, le=24.0)
    price: float = Field(..., ge=10.0, le=50000.0)
    description: Optional[str] = Field(default=None, max_length=300)


class AssistantZoneSurchargeItem(BaseModel):
    zone: str = Field(..., min_length=2, max_length=120)
    surcharge: float = Field(default=0.0, ge=0.0, le=10000.0)
    distance_km: Optional[float] = Field(default=None, ge=0.0, le=1000.0)


class MusicianAssistantConfigUpdate(BaseModel):
    enabled: bool = True
    greeting_message: Optional[str] = Field(default=None, max_length=500)
    packages: list[AssistantPackageItem] = Field(default_factory=list, max_length=20)
    additional_hour_price: Optional[float] = Field(default=None, ge=0.0, le=50000.0)
    zone_surcharges: list[AssistantZoneSurchargeItem] = Field(default_factory=list, max_length=30)
    per_km_rate: Optional[float] = Field(default=None, ge=0.0, le=500.0)
    night_surcharge_amount: float = Field(default=0.0, ge=0.0, le=10000.0)
    night_surcharge_start_hour: int = Field(default=23, ge=0, le=23)
    special_date_surcharge_percent: float = Field(default=0.0, ge=0.0, le=200.0)
    max_discount_amount: float = Field(default=0.0, ge=0.0, le=5000.0)
    max_discount_percent: float = Field(default=0.0, ge=0.0, le=50.0)
    custom_instructions: Optional[str] = Field(default=None, max_length=4000)


class MusicianAssistantConfigOut(MusicianAssistantConfigUpdate):
    model_config = ConfigDict(from_attributes=True)

    profile_id: UUID
    stage_name: Optional[str] = None
    profile_type: Optional[str] = "solo"
    base_price_per_hour: Optional[float] = None
    base_price_per_event: Optional[float] = None
    location_city: Optional[str] = None
    ai_engine_active: bool = True


class WhatsAppChannelConnectRequest(BaseModel):
    phone_number_id: str = Field(..., min_length=3, max_length=64)
    waba_id: Optional[str] = Field(default=None, max_length=64)
    display_phone_number: Optional[str] = Field(default=None, max_length=40)
    access_token: Optional[str] = Field(default=None, max_length=2048)


class WhatsAppChannelSettingsUpdate(BaseModel):
    is_active: Optional[bool] = None
    auto_pause_on_human_reply: Optional[bool] = None
    ignore_ensemble_members: Optional[bool] = None
    ignore_personal_chats: Optional[bool] = None
    ignored_phones: Optional[list[str]] = None


class WhatsAppChannelOut(BaseModel):
    connected: bool
    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    has_access_token: bool = False
    is_active: bool = True
    auto_pause_on_human_reply: bool = True
    ignore_ensemble_members: bool = True
    ignore_personal_chats: bool = True
    ignored_phones: list[str] = Field(default_factory=list)
    webhook_url: str
    verify_token: str
    meta_app_id: Optional[str] = None
    connected_at: Optional[datetime] = None


class AssistantUsageOut(BaseModel):
    """Uso del cupo mensual de IA del músico."""

    ai_calls_used: int
    ai_calls_limit: int
    audio_used: int
    audio_limit: int


class AssistantLimitsUpdate(BaseModel):
    """Topes de uso del modelo que ajusta el admin. 0 apaga esa vía."""

    monthly_ai_calls_per_musician: int = Field(..., ge=0, le=1_000_000)
    monthly_audio_per_musician: int = Field(..., ge=0, le=1_000_000)
    daily_ai_calls_global: int = Field(..., ge=0, le=1_000_000)


class AssistantLimitsOut(AssistantLimitsUpdate):
    global_calls_today: int = 0


class AssistantChatTurnRequest(BaseModel):
    conversation_id: Optional[UUID] = None
    profile_id: Optional[UUID] = None
    message: Optional[str] = Field(default=None, max_length=4000)
    # ~5 MB de audio en base64
    audio_base64: Optional[str] = Field(default=None, max_length=7_000_000)
    audio_mime_type: Optional[str] = Field(default="audio/webm", max_length=64)
    client_name: Optional[str] = Field(default="Cliente de Prueba", max_length=120)
    client_phone: Optional[str] = Field(default="+51999888777", max_length=40)


class AssistantMessageItem(BaseModel):
    id: str
    sender: Literal["client", "assistant", "musician"]
    text: str
    media_type: Literal["text", "audio"] = "text"
    created_at: str
    booking_link_token: Optional[str] = None
    # Cómo respondió el asistente (herramientas, fragmentos de conocimiento, vueltas). Opcional: la pantalla actual lo ignora.
    trace: Optional[dict] = None


class AssistantChatTurnOut(BaseModel):
    conversation_id: UUID
    profile_id: Optional[UUID] = None
    profile_name: Optional[str] = None
    status: str
    escalation_reason: Optional[str] = None
    reply: Optional[str] = None
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    quote_breakdown: Optional[dict[str, Any]] = None
    booking_link_token: Optional[str] = None
    booking_link_url: Optional[str] = None
    messages: list[AssistantMessageItem] = Field(default_factory=list)


class AssistantConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    profile_id: Optional[UUID] = None
    profile_name: Optional[str] = None
    channel: str
    client_phone: Optional[str] = None
    client_name: Optional[str] = None
    status: str
    escalation_reason: Optional[str] = None
    extracted_data: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    last_booking_link_token: Optional[str] = None
    last_message_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AssistantConversationStatusUpdate(BaseModel):
    status: Literal["bot_active", "paused_by_musician", "ignored_personal"]
