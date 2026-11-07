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



def musician_listed_sql_filters() -> list:
    """Equivalente en SQL de `musician_has_city_and_price` para paginar en la base de datos.

    `is_musician_listed_publicly` se sigue aplicando en Python como red de seguridad.
    """
    from sqlalchemy import and_, func, or_

    has_city = func.length(func.btrim(func.coalesce(MusicianProfile.location_city, ""))) > 0
    has_price = or_(
        and_(
            MusicianProfile.availability_type == AvailabilityType.hourly,
            MusicianProfile.price_per_hour.isnot(None),
        ),
        and_(
            MusicianProfile.availability_type == AvailabilityType.per_event,
            MusicianProfile.price_per_event.isnot(None),
        ),
        and_(
            or_(
                MusicianProfile.availability_type.is_(None),
                MusicianProfile.availability_type.notin_(
                    [AvailabilityType.hourly, AvailabilityType.per_event]
                ),
            ),
            or_(
                MusicianProfile.price_per_hour.isnot(None),
                MusicianProfile.price_per_event.isnot(None),
            ),
        ),
    )
    return [has_city, has_price]
