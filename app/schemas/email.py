from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EmailTemplateOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None
    subject: str
    html_body: str
    text_body: str
    available_variables: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EmailTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    html_body: str | None = Field(default=None, min_length=1, max_length=100000)
    text_body: str | None = Field(default=None, min_length=1, max_length=50000)
    enabled: bool | None = None


class EmailTemplatePreviewRequest(BaseModel):
    to: EmailStr | None = None
    context: dict[str, str] = Field(default_factory=dict)


class EmailTemplateRenderRequest(BaseModel):
    context: dict[str, str] = Field(default_factory=dict)
    subject: str | None = Field(default=None, max_length=200)
    html_body: str | None = Field(default=None, max_length=100000)
    text_body: str | None = Field(default=None, max_length=50000)


class EmailTemplateRenderOut(BaseModel):
    subject: str
    html: str
    text: str
    context: dict[str, str]


class EmailLogOut(BaseModel):
    id: UUID
    template_slug: str
    recipient: str
    subject: str
    status: str
    error_message: str | None
    provider_message_id: str | None
    user_id: UUID | None
    meta: dict | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class PasswordResetPreviewOut(BaseModel):
    email: EmailStr
    fullname: str


class EmailVerificationResult(BaseModel):
    message: str
    email_verified: bool = True
    password_reset_sent: bool = False
