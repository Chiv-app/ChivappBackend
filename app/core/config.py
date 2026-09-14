from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "MusicLink API"
    API_V1_STR: str = "/api/v1"

    SQLALCHEMY_DATABASE_URI: str

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    BACKEND_CORS_ORIGINS: list[str] = [
        "https://chiv.app",
        "https://www.chiv.app",
        "http://localhost:3000",
        "http://localhost:3002",
    ]

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    FACEBOOK_APP_ID: str = ""
    FACEBOOK_APP_SECRET: str = ""
    # Must match the browser-facing API base (Next.js proxy in local/dev).
    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:3000/api/v1"
    FRONTEND_URL: str = "http://localhost:3000"

    BREVO_API_KEY: str = ""
    EMAIL_FROM: str = "Chivapp <soporte@chiv.app>"
    EMAIL_ENABLED: bool = True

    # Rate Limiting
    RATE_LIMITING_ENABLED: bool = True
    RATE_LIMIT_LOGIN: str = "10/minute"
    RATE_LIMIT_REGISTER: str = "10/minute"
    RATE_LIMIT_PASSWORD_RESET: str = "5/minute"
    RATE_LIMIT_DEFAULT: str = "100/minute"

    # Security Headers
    ENABLE_HSTS: bool = False

    # Mercado Pago
    MERCADO_PAGO_ACCESS_TOKEN: str = ""
    MERCADO_PAGO_PUBLIC_KEY: str = ""
    MERCADO_PAGO_WEBHOOK_SECRET: str = ""
    MERCADO_PAGO_SANDBOX: bool = False
    # Public URL for webhooks (in production or ngrok/tunnel; falls back to OAUTH_REDIRECT_BASE_URL)
    MERCADO_PAGO_WEBHOOK_BASE_URL: str = ""

    # Google Cloud Storage
    GCP_BUCKET_NAME: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                import json

                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


settings = Settings()
