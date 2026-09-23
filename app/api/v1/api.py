from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth,
    availability,
    media,
    musician_search,
    profiles,
    bookings,
    booking_lifecycle,
    booking_share,
    contracts,
    payments,
    notifications,
    uploads,
    admin,
    admin_email,
    ensemble_members,
    booking_location,
    support,
    calendar_auth,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(profiles.router)
api_router.include_router(bookings.router)
api_router.include_router(booking_lifecycle.router)
api_router.include_router(booking_share.router)
api_router.include_router(booking_location.router)
api_router.include_router(contracts.router)
api_router.include_router(payments.router)
api_router.include_router(notifications.router)
api_router.include_router(availability.router)
api_router.include_router(media.router)
api_router.include_router(musician_search.router)
api_router.include_router(uploads.router)
api_router.include_router(admin.router)
api_router.include_router(admin_email.router)
api_router.include_router(ensemble_members.router)
api_router.include_router(support.router)
api_router.include_router(calendar_auth.router, prefix="/calendar-auth", tags=["calendar-auth"])
