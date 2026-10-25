from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Chivapp API"
    # "production" desactiva /docs y los orígenes CORS de desarrollo (ngrok, localhost).
    ENVIRONMENT: str = "development"
    API_V1_STR: str = "/api/v1"

    SQLALCHEMY_DATABASE_URI: str
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

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
    # Configurar explícitamente GOOGLE_CALENDAR_ID en el entorno cuando se habilite el Service Account.
    GOOGLE_CALENDAR_ID: str = ""
    GOOGLE_CREDENTIALS_JSON: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:3000/calendar-callback"
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
    RATE_LIMIT_OTP: str = "5/minute"
    RATE_LIMIT_REGISTER: str = "10/minute"
    RATE_LIMIT_PASSWORD_RESET: str = "5/minute"
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_PUBLIC: str = "30/minute"
    RATE_LIMIT_PAYMENT: str = "15/minute"
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

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() not in {
            "development",
            "dev",
            "local",
            "test",
            "testing",
        }

    @model_validator(mode="after")
    def validate_production_security(self):
        if self.is_production:
            secret = (self.JWT_SECRET_KEY or "").strip()
            lower_secret = secret.lower()
            weak_secrets = {
                "",
                "changeme",
                "secret",
                "supersecret",
                "your-secret-key",
                "dev-secret-key",
                "dev-secret-change-me",
                "test-secret",
            }
            if (
                len(secret) < 32
                or lower_secret in weak_secrets
                or lower_secret.startswith("change-me")
                or lower_secret.startswith("cambiar_por")
            ):
                raise ValueError(
                    "En producción JWT_SECRET_KEY debe tener al menos 32 caracteres y no ser un valor por defecto."
                )
            if "postgres:postgres@localhost" in self.SQLALCHEMY_DATABASE_URI:
                raise ValueError(
                    "En producción SQLALCHEMY_DATABASE_URI no puede usar las credenciales locales por defecto."
                )
            if self.MERCADO_PAGO_SANDBOX:
                raise ValueError(
                    "En producción MERCADO_PAGO_SANDBOX debe ser False."
                )
            if self.MERCADO_PAGO_ACCESS_TOKEN.strip() and not self.MERCADO_PAGO_WEBHOOK_SECRET.strip():
                raise ValueError(
                    "En producción MERCADO_PAGO_WEBHOOK_SECRET es obligatorio cuando Mercado Pago está activo."
                )
            proxy_secret = self.PROXY_SHARED_SECRET.strip()
            proxy_secret_lower = proxy_secret.lower()
            if (
                not proxy_secret
                or len(proxy_secret) < 32
                or proxy_secret_lower in weak_secrets
                or proxy_secret_lower.startswith("cambiar_por")
                or proxy_secret_lower.startswith("change-me")
            ):
                raise ValueError(
                    "En producción PROXY_SHARED_SECRET es obligatorio, debe tener al menos 32 caracteres y no usar un valor de plantilla."
                )
            private_bucket = (self.GCP_PRIVATE_BUCKET_NAME or "").strip()
            public_bucket = (self.GCP_BUCKET_NAME or "").strip()
            if not private_bucket or (public_bucket and private_bucket == public_bucket):
                raise ValueError(
                    "En producción GCP_PRIVATE_BUCKET_NAME es obligatorio y debe ser distinto de GCP_BUCKET_NAME."
                )
            rate_limit_uri = (self.RATE_LIMIT_STORAGE_URI or "").strip().lower()
            if not rate_limit_uri.startswith(("redis://", "rediss://")):
                raise ValueError(
                    "En producción RATE_LIMIT_STORAGE_URI debe apuntar a Redis (redis:// o rediss://)."
                )
            frontend_url = (self.FRONTEND_URL or "").strip().lower()
            if not frontend_url.startswith("https://"):
                raise ValueError(
                    "En producción FRONTEND_URL debe usar https://."
                )
            self.BACKEND_CORS_ORIGINS = [
                origin
                for origin in self.BACKEND_CORS_ORIGINS
                if "localhost" not in origin.lower() and "127.0.0.1" not in origin
            ]
        return self


settings = Settings()
