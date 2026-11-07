from sqlalchemy.orm import Session

from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus


def is_profile_public(profile: MusicianProfile | ContractorProfile) -> bool:
    """Approved by admin: published status + user verification flag."""
    user = getattr(profile, "user", None)
    return (
        profile.status == ProfileStatus.published
        and user is not None
        and bool(user.is_verified)
    )


def musician_has_price(profile: MusicianProfile) -> bool:
    """Tiene tarifa según su modalidad (por hora, por evento o ambas)."""
    if profile.availability_type == AvailabilityType.hourly:
        return profile.price_per_hour is not None
    if profile.availability_type == AvailabilityType.per_event:
        return profile.price_per_event is not None
    return profile.price_per_hour is not None or profile.price_per_event is not None


def musician_has_city_and_price(profile: MusicianProfile) -> bool:
    """Ciudad y tarifa son obligatorias para aparecer en el marketplace."""
    return bool((profile.location_city or "").strip()) and musician_has_price(profile)


def is_musician_listed_publicly(db: Session, profile: MusicianProfile) -> bool:
    """
    Marketplace-ready musicians: verified and published by admin, con ciudad y tarifa.
    Used for home, search, direct detail and public discovery.
    """
    return is_profile_public(profile) and musician_has_city_and_price(profile)

