from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class UserOut(BaseModel):
    id: UUID
    email: EmailStr
    username: str | None = None
    fullname: str | None = None
    role: str
    phone: str | None
    profile_picture_url: str | None
    is_verified: bool
    is_active: bool = True
    is_email_verified: bool = False
    has_password: bool = False
    has_connected_calendar: bool = False
    # True si el usuario figura como integrante de alguna agrupación
    is_ensemble_member: bool = False
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="wrap")
    @classmethod
    def populate_has_password(cls, value, handler):
        # Accept ORM User (with password_hash) without exposing the hash.
        if hasattr(value, "password_hash") and not isinstance(value, dict):
            data = {
                "id": value.id,
                "email": value.email,
                "username": getattr(value, "username", None),
                "fullname": value.fullname,
                "role": value.role.value if hasattr(value.role, "value") else value.role,
                "phone": value.phone,
                "profile_picture_url": value.profile_picture_url,
                "is_verified": value.is_verified,
                "is_active": getattr(value, "is_active", True),
                "is_email_verified": value.email_verified_at is not None,
                "has_password": bool(value.password_hash),
                "has_connected_calendar": bool(getattr(value, "google_calendar_refresh_token", None)),
                "is_ensemble_member": bool(
                    getattr(value, "is_ensemble_member", False)
                ),
                "created_at": value.created_at,
                "updated_at": value.updated_at,
                "last_login_at": value.last_login_at,
            }
            return handler(data)
        if isinstance(value, dict) and "has_password" not in value:
            value = {
                **value,
                "has_password": bool(value.get("password_hash")),
                "has_connected_calendar": bool(value.get("google_calendar_refresh_token")),
            }
            value.pop("password_hash", None)
        return handler(value)




