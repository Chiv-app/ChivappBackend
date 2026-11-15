"""Tipo de perfil (solista o grupo) y datos de detalle del perfil artístico.

Los valores permitidos viven aquí y son los mismos que ofrece la guía de alta: lo que no esté en las
listas se rechaza, de modo que el buscador y el perfil público trabajen con claves estables.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_profile import MusicianProfile
from app.models.user import User

ProfileType = Literal["solo", "group"]
GroupSize = Literal["2", "3", "4-5", "6-8", "9+"]

PROFILE_TYPES: tuple[str, ...] = ("solo", "group")
GROUP_SIZES: tuple[str, ...] = ("2", "3", "4-5", "6-8", "9+")

EventType = Literal[
    "bodas",
    "cumpleanos",
    "quinceaneros",
    "bautizos",
    "aniversarios",
    "infantiles",
    "empresa",
    "inauguraciones",
    "restaurantes",
]
Audience = Literal["50", "150", "300", "300+", "any"]
Duration = Literal["1h", "2h", "3h", "4h+", "flex"]
Include = Literal["sound", "lights", "transport", "outfit", "requests", "mc"]
Setup = Literal["30m", "1h", "2h+"]
TravelFee = Literal["yes", "no", "maybe"]
Notice = Literal["0", "2d", "1w", "2w", "1m"]
Experience = Literal["<2", "2-5", "5-10", "10+"]
GroupFormat = Literal["acoustic", "electric", "orchestra", "mixed"]
YesNo = Literal["yes", "no"]

CityText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]

# Claves que solo tienen sentido en un grupo: al pasar a solista se descartan.
GROUP_ONLY_DETAIL_KEYS: tuple[str, ...] = ("format", "reduced")

# Campos que el propio músico completa sin que el equipo vuelva a revisar el perfil.
SELF_SERVICE_FIELDS: frozenset[str] = frozenset({"profile_type", "group_size", "details"})


class ProfileDetails(BaseModel):
    """Datos opcionales del perfil. Se guardan completos: lo que no se envía queda sin completar."""

    model_config = ConfigDict(extra="forbid")

    event_types: list[EventType] = Field(default_factory=list, max_length=9)
    audience: Audience | None = None
    duration: Duration | None = None
    includes: list[Include] = Field(default_factory=list, max_length=6)
    setup: Setup | None = None
    other_cities: list[CityText] = Field(default_factory=list, max_length=10)
    travel_fee: TravelFee | None = None
    notice: Notice | None = None
    experience: Experience | None = None
    format: GroupFormat | None = None
    reduced: YesNo | None = None


def clean_details(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Quita claves vacías y repetidos para guardar solo lo que la persona completó."""
    cleaned: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            seen: list[Any] = []
            for item in value:
                if item not in seen:
                    seen.append(item)
            value = seen
        cleaned[key] = value
    return cleaned


def count_open_members(db: Session, user: User, profile: MusicianProfile | None = None) -> int:
    """Integrantes que tiene hoy el perfil (invitados o activos). Sin perfil, los de toda la persona."""
    from app.services.musician_profiles import members_of_profile_clause

    query = db.query(func.count(EnsembleMember.id)).filter(
        EnsembleMember.leader_user_id == user.id,
        EnsembleMember.status.in_([EnsembleMemberStatus.invited, EnsembleMemberStatus.active]),
    )
    if profile is not None:
        query = query.filter(members_of_profile_clause(profile))
    return query.scalar() or 0


def normalize_type_and_details(
    db: Session,
    user: User,
    updates: dict[str, Any],
    *,
    current_type: str = "solo",
    current_group_size: str | None = None,
    current_details: dict[str, Any] | None = None,
    profile: MusicianProfile | None = None,
) -> dict[str, Any]:
    """Deja coherentes el tipo, el tamaño del grupo y los detalles antes de guardar.

    - Un solista no tiene tamaño de grupo ni datos exclusivos de grupo.
    - No se puede pasar a solista mientras haya integrantes: primero se quitan.
    """
    if not ({"profile_type", "group_size", "details"} & updates.keys()):
        return updates

    result = dict(updates)
    effective_type = result.get("profile_type") or current_type

    if "details" in result:
        result["details"] = clean_details(result["details"])

    if effective_type == "solo":
        if current_type == "group" and result.get("profile_type") == "solo" and count_open_members(db, user, profile) > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tu perfil tiene integrantes. Quítalos antes de pasar a solista.",
            )
        if current_group_size is not None or result.get("group_size") is not None:
            result["group_size"] = None
        details = result["details"] if "details" in result else clean_details(current_details)
        if any(key in details for key in GROUP_ONLY_DETAIL_KEYS):
            result["details"] = {k: v for k, v in details.items() if k not in GROUP_ONLY_DETAIL_KEYS}
        elif "details" in result:
            result["details"] = details
    return result
