from pydantic import BaseModel, EmailStr, Field, model_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    fullname: str | None = Field(default=None, max_length=150)
    role: str = Field(..., max_length=32)
    username: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=30)
    accepted_terms: bool = False

    @model_validator(mode="after")
    def check_contractor_requirements(self) -> "RegisterRequest":
        if self.role == "contractor":
            if not self.fullname:
                raise ValueError("El nombre completo es requerido para contratistas.")
            if not self.phone:
                raise ValueError("El número de celular es requerido para contratistas.")
        return self


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    token: str | None = Field(default=None, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordSetupPreviewOut(BaseModel):
    email: EmailStr
    fullname: str
    specialties: list[str] = Field(default_factory=list)
    leader_name: str | None = None
    requires_password: bool = True


class TokenOut(BaseModel):
    # El token de sesión solo viaja en la cookie httponly.
    message: str = "Sesión iniciada"


class OAuthCompleteRequest(BaseModel):
    role: str = Field(..., max_length=32)
    username: str | None = Field(default=None, max_length=64)
    phone: str | None = Field(default=None, max_length=30)


class OAuthAccountOut(BaseModel):
    provider: str
    email: str | None = None
    linked: bool = True


class OAuthAccountsOut(BaseModel):
    accounts: list[OAuthAccountOut]
    has_password: bool


class OAuthPendingOut(BaseModel):
    email: str | None = None
    fullname: str
    provider: str
    picture_url: str | None = None


class GoogleCredentialRequest(BaseModel):
    credential: str = Field(..., max_length=8192)
    intent: str = Field(default="login", max_length=32)


class GoogleAuthResponse(BaseModel):
    status: str
    access_token: str | None = None
    redirect_url: str | None = None
    role: str | None = None


class GuestRegisterRequest(BaseModel):
    email: EmailStr
    fullname: str = Field(..., min_length=2, max_length=150)
    phone: str = Field(..., min_length=6, max_length=30)


class LoginStepOneRequest(BaseModel):
    email: EmailStr


class LoginStepOneResponse(BaseModel):
    status: str
    message: str | None = None


class LoginOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=4, max_length=12)


class VerifyEmailRequest(BaseModel):
    password: str | None = Field(default=None, max_length=128)


class MagicLinkConsumeRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=2048)
    redirect: str | None = Field(default=None, max_length=512)
