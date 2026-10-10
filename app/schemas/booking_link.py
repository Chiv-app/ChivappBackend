from datetime import date, time, datetime
from pydantic import BaseModel, EmailStr, Field
from uuid import UUID

class BookingLinkCreate(BaseModel):
    price: float = Field(gt=0, description="Precio total del evento")
    event_date: date | None = None
    start_time: time | None = None
    client_email: EmailStr | None = None

class BookingLinkOut(BaseModel):
    id: UUID
    token: str
    musician_id: UUID
    price: float
    event_date: date | None
    start_time: time | None
    client_email: EmailStr | None
    status: str
    expires_at: datetime
    booking_id: UUID | None
    created_at: datetime

    class Config:
        from_attributes = True

class MusicianLinkProfile(BaseModel):
    id: UUID
    stage_name: str
    profile_picture_url: str | None
    
    class Config:
        from_attributes = True

class BookingLinkPublicOut(BaseModel):
    token: str
    price: float
    event_date: date | None
    start_time: time | None
    musician: MusicianLinkProfile
    status: str
    expires_at: datetime

class BookingLinkCheckout(BaseModel):
    # Accordion 1: Event Details
    event_date: date
    start_time: time
    event_type: str
    location_address: str
    location_city: str | None = None
    
    # Accordion 2: Client Info
    fullname: str
    email: EmailStr
    phone: str
    document_number: str | None = None
