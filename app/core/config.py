from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Chivapp API"
    # "production" desactiva /docs y los orígenes CORS de desarrollo (ngrok, localhost).
    ENVIRONMENT: str = "development"
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
    # TODO: mover a la variable de entorno GOOGLE_CALENDAR_ID en producción y
    # quitar este valor por defecto (es un correo personal).
    GOOGLE_CALENDAR_ID: str = "miguel2cto@gmail.com"
    GOOGLE_CREDENTIALS_JSON: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:3000/calendar/callback"
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
    RATE_LIMIT_PUBLIC: str = "30/minute"
    # memory:// es por proceso; en producción con varias instancias usar redis://...
    RATE_LIMIT_STORAGE_URI: str = "memory://"
    # Secreto compartido con el proxy de Next.js para confiar en la IP del cliente
    # que reenvía (cabecera x-chivapp-client-ip).
    PROXY_SHARED_SECRET: str = ""

    # Security Headers
    ENABLE_HSTS: bool = False

    # Mercado Pago
    MERCADO_PAGO_ACCESS_TOKEN: str = ""
    MERCADO_PAGO_PUBLIC_KEY: str = ""
    MERCADO_PAGO_WEBHOOK_SECRET: str = ""
    MERCADO_PAGO_SANDBOX: bool = False
    # Public URL for webhooks (in production or ngrok/tunnel; falls back to OAUTH_REDIRECT_BASE_URL)
    MERCADO_PAGO_WEBHOOK_BASE_URL: str = ""

    # Política de cancelación (cuando cancela el contratista con el pago retenido).
    # Días de anticipación al evento -> % de reembolso sobre lo pagado sin el costo de pasarela.
    CANCEL_FULL_REFUND_MIN_DAYS: int = 15  # más de 15 días: 100 %
    CANCEL_PARTIAL_REFUND_MIN_DAYS: int = 7  # entre 7 y 15 días: % parcial
    CANCEL_PARTIAL_REFUND_PERCENT: int = 50  # menos de 7 días: 0 %

    # Google Cloud Storage
    GCP_BUCKET_NAME: str = ""
    # Bucket PRIVADO (sin acceso público) para DNI, firmas y comprobantes.
    GCP_PRIVATE_BUCKET_NAME: str = ""

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
