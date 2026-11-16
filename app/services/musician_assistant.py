"""Motor Multi-Tenant del Asistente IA por perfil de músico y autocompletado por voz/texto.

Alimenta al asistente con:
1. La ficha real de `MusicianProfile` (nombre artístico, tipo solista/grupo, precios base, repertorio, videos, ciudad).
2. El entrenamiento de precios `MusicianAssistantConfig` (paquetes por integrantes/duración, zonas, recargo nocturno,
   descuento máximo permitido e instrucciones libres).
3. El calendario real de la persona (`assert_musician_available` + Google Calendar `freebusy`).
4. Generación directa de enlaces de contrato pre-llenados (`BookingLink` -> `/pay/{token}`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.booking_link import BookingLink, BookingLinkStatus
from app.models.ensemble_member import EnsembleMember, EnsembleMemberStatus
from app.models.musician_assistant import (
    AssistantConversation,
    MusicianAssistantConfig,
    MusicianWhatsAppChannel,
)
from app.models.musician_profile import MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User
from app.services.assistant_usage import AssistantQuotaExceeded, consume_ai_call
from app.services.availability_match import assert_musician_available
from app.services.booking_notifications import notify_user
from app.services.calendar_service import get_busy_blocks
from app.services.musician_profiles import list_user_profiles, resolve_active_profile
from app.services.person_calendar import CalendarRange, list_calendar_events, subtract_synced_events
from app.services.uniqueness import normalize_phone

logger = logging.getLogger(__name__)

LIMA_TZ = ZoneInfo("America/Lima")
_WA_ENC_PREFIX = "wa:v1:"


def get_lima_now() -> datetime:
    return datetime.now(LIMA_TZ)


def _whatsapp_fernet() -> Fernet:
    base_secret = (getattr(settings, "JWT_SECRET_KEY", None) or "chivapp-default-secret").strip()
    derived = hmac.new(
        base_secret.encode("utf-8"),
        b"chivapp:whatsapp-token-fernet:v1",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_whatsapp_token(token: str | None) -> str | None:
    if not token or not token.strip():
        return None
    clean = token.strip()
    if clean.startswith(_WA_ENC_PREFIX):
        return clean
    cipher = _whatsapp_fernet().encrypt(clean.encode("utf-8")).decode("ascii")
    return f"{_WA_ENC_PREFIX}{cipher}"


def decrypt_whatsapp_token(value: str | None) -> str | None:
    if not value:
        return None
    if not value.startswith(_WA_ENC_PREFIX):
        return value
    raw = value[len(_WA_ENC_PREFIX) :]
    try:
        return _whatsapp_fernet().decrypt(raw.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("No se pudo descifrar access_token_encrypted de WhatsApp.")
        return None


def _default_packages_for_profile(profile: MusicianProfile) -> list[dict[str, Any]]:
    hourly = float(profile.price_per_hour) if profile.price_per_hour is not None else 0.0
    per_event = float(profile.price_per_event) if profile.price_per_event is not None else 0.0
    base_price = hourly if hourly >= 10 else (per_event if per_event >= 10 else 450.0)

    is_group = (profile.profile_type or "solo") == "group"
    default_members = 5
    if profile.group_size == "2":
        default_members = 2
    elif profile.group_size == "3":
        default_members = 3
    elif profile.group_size == "4-5":
        default_members = 5
    elif profile.group_size == "6-8":
        default_members = 7
    elif profile.group_size == "9+":
        default_members = 9

    if not is_group:
        return [
            {
                "id": "pkg-solo-1h",
                "name": "Presentación solista (1 hora)",
                "members_count": 1,
                "duration_hours": 1.0,
                "price": round(base_price, 2),
                "description": "Show musical en vivo de 1 hora.",
            }
        ]

    packages: list[dict[str, Any]] = [
        {
            "id": "pkg-group-standard",
            "name": f"Show estándar ({default_members} integrantes - 1 hora)",
            "members_count": default_members,
            "duration_hours": 1.0,
            "price": round(base_price, 2),
            "description": f"Agrupación con {default_members} músicos en escena por 1 hora.",
        }
    ]
    if default_members >= 4:
        reduced_members = max(3, default_members - 1)
        reduced_price = max(10.0, round(base_price * 0.85, 2))
        packages.insert(
            0,
            {
                "id": "pkg-group-reduced",
                "name": f"Formato reducido ({reduced_members} integrantes - 1 hora)",
                "members_count": reduced_members,
                "duration_hours": 1.0,
                "price": reduced_price,
                "description": f"Formato compacto con {reduced_members} integrantes.",
            },
        )
    return packages


def get_or_create_assistant_config(
    db: Session,
    profile: MusicianProfile,
) -> MusicianAssistantConfig:
    config = (
        db.query(MusicianAssistantConfig)
        .filter(MusicianAssistantConfig.profile_id == profile.id)
        .first()
    )
    if config is None:
        default_zones = []
        if profile.location_city:
            default_zones.append(
                {
                    "zone": profile.location_city.strip(),
                    "surcharge": 0.0,
                    "distance_km": 0.0,
                }
            )
        config = MusicianAssistantConfig(
            profile_id=profile.id,
            enabled=True,
            greeting_message=(
                f"¡Hola! Soy el asistente de {profile.stage_name or 'nuestra agrupación'}. "
                "Cuéntame para qué fecha, hora, lugar y tipo de evento deseas cotizar."
            ),
            packages=_default_packages_for_profile(profile),
            additional_hour_price=(
                float(profile.price_per_hour)
                if profile.price_per_hour is not None
                else None
            ),
            zone_surcharges=default_zones,
            night_surcharge_amount=Decimal("0.00"),
            night_surcharge_start_hour=23,
            special_date_surcharge_percent=Decimal("0.00"),
            max_discount_amount=Decimal("0.00"),
            max_discount_percent=Decimal("0.00"),
            custom_instructions=None,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def get_or_create_whatsapp_channel(
    db: Session,
    user: User,
) -> MusicianWhatsAppChannel:
    channel = (
        db.query(MusicianWhatsAppChannel)
        .filter(MusicianWhatsAppChannel.user_id == user.id)
        .first()
    )
    if channel is None:
        channel = MusicianWhatsAppChannel(
            user_id=user.id,
            webhook_verify_token=secrets.token_urlsafe(16),
            is_active=True,
            auto_pause_on_human_reply=True,
            ignore_ensemble_members=True,
            ignore_personal_chats=True,
            ignored_phones=[],
        )
        db.add(channel)
        db.commit()
        db.refresh(channel)
    return channel


def _is_special_peru_date(target_date: date | None) -> tuple[bool, str | None]:
    if target_date is None:
        return False, None
    md = (target_date.month, target_date.day)
    fixed_holidays = {
        (1, 1): "Año Nuevo",
        (2, 14): "Día de San Valentín",
        (7, 28): "Fiestas Patrias",
        (7, 29): "Fiestas Patrias",
        (12, 24): "Nochebuena",
        (12, 25): "Navidad",
        (12, 31): "Fin de Año",
    }
    if md in fixed_holidays:
        return True, fixed_holidays[md]
    # Segundo domingo de mayo (Día de la Madre) y víspera
    if target_date.month == 5 and target_date.weekday() in (5, 6) and 8 <= target_date.day <= 14:
        return True, "Día de la Madre"
    # Tercer domingo de junio (Día del Padre) y víspera
    if target_date.month == 6 and target_date.weekday() in (5, 6) and 15 <= target_date.day <= 21:
        return True, "Día del Padre"
    return False, None


def calculate_smart_quote(
    profile: MusicianProfile,
    config: MusicianAssistantConfig,
    *,
    members_count: int | None = None,
    duration_hours: float | None = None,
    zone_or_city: str | None = None,
    address: str | None = None,
    distance_km: float | None = None,
    start_time: time | None = None,
    event_date: date | None = None,
    requested_discount: bool = False,
    explicit_price: float | None = None,
) -> dict[str, Any]:
    """Calcula el precio inteligente según integrantes, duración, zona/distancia, hora y fechas especiales."""
    packages = list(config.packages or []) or _default_packages_for_profile(profile)
    eff_hours = float(duration_hours) if duration_hours and duration_hours > 0 else 1.0

    # 1. Selección de paquete según cantidad de integrantes o duración
    chosen_pkg = packages[0]
    if members_count is not None and len(packages) > 1:
        chosen_pkg = min(
            packages,
            key=lambda p: (
                abs(int(p.get("members_count", 1)) - members_count),
                abs(float(p.get("duration_hours", 1.0)) - eff_hours),
            ),
        )
    elif len(packages) > 1:
        # Preferir el paquete estándar (el de mayor integrantes o el de 1h)
        chosen_pkg = max(packages, key=lambda p: int(p.get("members_count", 1)))

    pkg_price = float(chosen_pkg.get("price", 400.0))
    pkg_hours = float(chosen_pkg.get("duration_hours", 1.0) or 1.0)
    pkg_members = int(chosen_pkg.get("members_count", 1) or 1)
    pkg_name = str(chosen_pkg.get("name") or f"Show ({pkg_members} integrantes)")

    # 2. Horas adicionales
    extra_hours = max(0.0, eff_hours - pkg_hours)
    hourly_extra_rate = (
        float(config.additional_hour_price)
        if config.additional_hour_price is not None and float(config.additional_hour_price) > 0
        else (
            float(profile.price_per_hour)
            if profile.price_per_hour is not None and float(profile.price_per_hour) > 0
            else round(pkg_price / max(pkg_hours, 1.0), 2)
        )
    )
    extra_hours_amount = round(extra_hours * hourly_extra_rate, 2)

    # 3. Zona / distancia y verificación de cobertura
    zone_surcharge = 0.0
    matched_zone_label: str | None = None
    out_of_coverage = False

    location_haystack = " ".join(filter(None, [zone_or_city, address])).strip().lower()
    configured_zones = list(config.zone_surcharges or [])

    if location_haystack and configured_zones:
        for z in configured_zones:
            z_name = str(z.get("zone") or "").strip()
            if z_name and z_name.lower() in location_haystack:
                zone_surcharge = float(z.get("surcharge") or 0.0)
                matched_zone_label = z_name
                break

    if matched_zone_label is None and distance_km is not None and distance_km > 0 and config.per_km_rate:
        zone_surcharge = round(float(distance_km) * float(config.per_km_rate), 2)
        matched_zone_label = f"{distance_km:g} km"

    # Detectar si menciona otra ciudad lejana fuera de cobertura
    base_city = (profile.location_city or "").strip().lower()
    other_cities = [
        str(c).strip().lower()
        for c in ((profile.details or {}).get("other_cities") or [])
        if str(c).strip()
    ]
    known_zones = [str(z.get("zone") or "").strip().lower() for z in configured_zones if z.get("zone")]
    allowed_places = {p for p in [base_city, *other_cities, *known_zones] if p}

    distant_cities = {
        "trujillo",
        "arequipa",
        "piura",
        "chiclayo",
        "iquitos",
        "tacna",
        "puno",
        "tumbes",
        "tarapoto",
        "pucallpa",
        "cajamarca",
        "chimbote",
    }
    if location_haystack and matched_zone_label is None:
        for dc in distant_cities:
            if dc in location_haystack and dc not in allowed_places:
                out_of_coverage = True
                break

    # 4. Recargo nocturno / madrugada
    night_surcharge = 0.0
    night_amount_cfg = float(config.night_surcharge_amount or 0.0)
    night_start_hour = int(config.night_surcharge_start_hour if config.night_surcharge_start_hour is not None else 23)
    if start_time is not None and night_amount_cfg > 0:
        hr = start_time.hour
        is_night = (hr >= night_start_hour) or (0 <= hr <= 5)
        if is_night:
            night_surcharge = round(night_amount_cfg, 2)

    # 5. Fecha especial
    is_special, special_label = _is_special_peru_date(event_date)
    special_pct = float(config.special_date_surcharge_percent or 0.0)
    subtotal_before_special = pkg_price + extra_hours_amount + zone_surcharge + night_surcharge
    special_date_surcharge = (
        round(subtotal_before_special * (special_pct / 100.0), 2)
        if (is_special and special_pct > 0)
        else 0.0
    )

    gross_total = round(subtotal_before_special + special_date_surcharge, 2)

    # 6. Descuento si el cliente regatea
    discount_applied = 0.0
    if requested_discount:
        max_amt = float(config.max_discount_amount or 0.0)
        max_pct = float(config.max_discount_percent or 0.0)
        pct_amt = round(gross_total * (max_pct / 100.0), 2) if max_pct > 0 else 0.0
        candidates = [d for d in (max_amt, pct_amt) if d > 0]
        if candidates:
            discount_applied = min(max(candidates), max(0.0, gross_total - 10.0))

    final_price = round(max(10.0, gross_total - discount_applied), 2)
    if explicit_price is not None and explicit_price >= 10.0:
        final_price = round(float(explicit_price), 2)

    parts = [f"{pkg_name}: S/ {pkg_price:.2f}"]
    if extra_hours_amount > 0:
        parts.append(f"+{extra_hours:g}h adicional(es): S/ {extra_hours_amount:.2f}")
    if zone_surcharge > 0:
        parts.append(f"Movilidad ({matched_zone_label or 'zona'}): S/ {zone_surcharge:.2f}")
    if night_surcharge > 0:
        parts.append(f"Horario nocturno: S/ {night_surcharge:.2f}")
    if special_date_surcharge > 0 and special_label:
        parts.append(f"Fecha especial ({special_label}): S/ {special_date_surcharge:.2f}")
    if discount_applied > 0:
        parts.append(f"Descuento especial aplicado: -S/ {discount_applied:.2f}")

    return {
        "package_name": pkg_name,
        "members_count": pkg_members,
        "duration_hours": eff_hours,
        "base_price": pkg_price,
        "extra_hours_amount": extra_hours_amount,
        "zone_surcharge": zone_surcharge,
        "matched_zone": matched_zone_label,
        "night_surcharge": night_surcharge,
        "special_date_surcharge": special_date_surcharge,
        "special_date_label": special_label,
        "discount_applied": discount_applied,
        "final_price": final_price,
        "out_of_coverage": out_of_coverage,
        "summary": " | ".join(parts) + f" → Total: S/ {final_price:.2f}",
    }


def check_profile_availability_summary(
    db: Session,
    profile: MusicianProfile,
    event_date: date,
    start_time: time,
    end_time: time | None = None,
) -> tuple[bool, str]:
    """Verifica disponibilidad en Chivapp (todos los perfiles + convocatorias) y Google Calendar."""
    try:
        assert_musician_available(
            db,
            profile.id,
            event_date,
            start_time,
            end_time=end_time,
        )
    except HTTPException as exc:
        return False, str(exc.detail)

    # También revisar bloques ocupados en Google Calendar si el usuario lo tiene conectado
    user = profile.user or db.get(User, profile.user_id)
    if user is not None:
        try:
            rng = CalendarRange(start=event_date, end=event_date)
            res = get_busy_blocks(
                user,
                datetime.combine(event_date, time.min),
                datetime.combine(event_date + timedelta(days=1), time.min),
            )
            busy = res.busy
            if busy:
                busy = subtract_synced_events(busy, list_calendar_events(db, user, rng))
            req_start = datetime.combine(event_date, start_time)
            req_end = (
                datetime.combine(event_date, end_time)
                if end_time and end_time > start_time
                else req_start + timedelta(hours=1)
            )
            for block in busy:
                b_start = datetime.fromisoformat(block["start"]).replace(tzinfo=None)
                b_end = datetime.fromisoformat(block["end"]).replace(tzinfo=None)
                if req_start < b_end and b_start < req_end:
                    return False, "El músico tiene ese horario ocupado en su calendario."
        except Exception:
            pass

    return True, "Disponible"


# --- NLU Extractor (Español Perú + Gemini Interactions API) ---

_WEEKDAY_MAP = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}

_MONTH_MAP = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_EVENT_TYPES_KEYWORDS = [
    ("quinceañero", "Quinceañero"),
    ("quinceanero", "Quinceañero"),
    ("15 años", "Quinceañero"),
    ("cumpleaños", "Cumpleaños"),
    ("cumpleanos", "Cumpleaños"),
    ("cumple", "Cumpleaños"),
    ("serenata", "Serenata"),
    ("matrimonio", "Matrimonio / Boda"),
    ("boda", "Matrimonio / Boda"),
    ("aniversario", "Aniversario"),
    ("bautizo", "Bautizo"),
    ("graduación", "Graduación"),
    ("graduacion", "Graduación"),
    ("corporativo", "Evento corporativo"),
    ("empresa", "Evento corporativo"),
    ("inauguración", "Inauguración"),
    ("inauguracion", "Inauguración"),
    ("pedida de mano", "Pedida de mano"),
]


def _extract_fields_heuristic(text: str, today: date | None = None) -> dict[str, Any]:
    """Extrae fecha, hora, duración, integrantes, evento, dirección, cliente, teléfono y precio desde texto en español."""
    base_today = today or get_lima_now().date()
    raw = (text or "").strip()
    lower = raw.lower()
    out: dict[str, Any] = {}

    # 1. Fecha (ISO YYYY-MM-DD, DD/MM/YYYY, "22 de noviembre", "mañana", "este sábado", etc.)
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", lower)
    slash_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}|\d{2}))?\b", lower)
    named_date_match = re.search(
        r"\b(\d{1,2})\s+(?:de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto|setiembre|septiembre|octubre|noviembre|diciembre)(?:\s+del?\s+(20\d{2}))?\b",
        lower,
    )
    weekday_num_match = re.search(
        r"\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+(\d{1,2})(?!\s*(?:am|pm|de la|horas|:))\b",
        lower,
    )

    if iso_match:
        try:
            out["event_date"] = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))).isoformat()
        except ValueError:
            pass
    elif named_date_match:
        day_num = int(named_date_match.group(1))
        month_num = _MONTH_MAP[named_date_match.group(2)]
        year_num = int(named_date_match.group(3)) if named_date_match.group(3) else base_today.year
        try:
            cand = date(year_num, month_num, day_num)
            if cand < base_today and not named_date_match.group(3):
                cand = date(year_num + 1, month_num, day_num)
            out["event_date"] = cand.isoformat()
        except ValueError:
            pass
    elif slash_match:
        d_num, m_num = int(slash_match.group(1)), int(slash_match.group(2))
        y_raw = slash_match.group(3)
        y_num = (2000 + int(y_raw) if len(y_raw) == 2 else int(y_raw)) if y_raw else base_today.year
        try:
            out["event_date"] = date(y_num, m_num, d_num).isoformat()
        except ValueError:
            pass
    elif weekday_num_match:
        day_num = int(weekday_num_match.group(2))
        try:
            cand = date(base_today.year, base_today.month, day_num)
            if cand < base_today:
                next_month = 1 if base_today.month == 12 else base_today.month + 1
                next_year = base_today.year + 1 if base_today.month == 12 else base_today.year
                cand = date(next_year, next_month, day_num)
            out["event_date"] = cand.isoformat()
        except ValueError:
            pass
    elif "pasado mañana" in lower or "pasado manana" in lower:
        out["event_date"] = (base_today + timedelta(days=2)).isoformat()
    elif "mañana" in lower or "manana" in lower:
        # Evitar confundir "en la mañana" con el día "mañana"
        if not re.search(r"\b(de|por|en)\s+la\s+ma[ñn]ana\b", lower):
            out["event_date"] = (base_today + timedelta(days=1)).isoformat()
    elif re.search(r"\bhoy\b", lower):
        out["event_date"] = base_today.isoformat()
    else:
        for w_name, w_idx in _WEEKDAY_MAP.items():
            if re.search(rf"\b{w_name}\b", lower):
                days_ahead = (w_idx - base_today.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                out["event_date"] = (base_today + timedelta(days=days_ahead)).isoformat()
                break

    # Si dice un día de la semana y una fecha que no coinciden («sábado 20 de noviembre» cuando el 20 cae viernes),
    # no se adivina: se guarda el conflicto para preguntarle cuál quiso decir.
    spoken_weekday = re.search(r"\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\b", lower)
    if spoken_weekday and out.get("event_date") and (named_date_match or weekday_num_match or iso_match or slash_match):
        weekday_idx = _WEEKDAY_MAP[spoken_weekday.group(1)]
        said_date = date.fromisoformat(out["event_date"])
        if said_date.weekday() != weekday_idx:
            out["date_conflict"] = {"weekday": spoken_weekday.group(1), "date": out.pop("event_date")}

    # 2. Hora (ej. "10 pm", "10:30pm", "a las 22:00", "a las 11 de la noche")
    time_match = re.search(
        r"\b(?:a\s+las\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|de\s+la\s+noche|de\s+la\s+tarde|de\s+la\s+ma[ñn]ana|de\s+la\s+madrugada)\b",
        lower,
    )
    time_24_match = re.search(r"\b(?:a\s+las\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", lower)
    if time_match:
        hr = int(time_match.group(1))
        mn = int(time_match.group(2) or 0)
        mer = time_match.group(3)
        if any(k in mer for k in ("pm", "p.m", "noche", "tarde")) and hr < 12:
            hr += 12
        elif any(k in mer for k in ("am", "a.m", "madrugada", "mañana", "manana")) and hr == 12:
            hr = 0
        if 0 <= hr <= 23 and 0 <= mn <= 59:
            out["start_time"] = f"{hr:02d}:{mn:02d}"
    elif time_24_match:
        hr = int(time_24_match.group(1))
        mn = int(time_24_match.group(2))
        out["start_time"] = f"{hr:02d}:{mn:02d}"

    # 3. Duración en horas
    if "media hora" in lower or "30 min" in lower:
        out["duration_hours"] = 0.5
    else:
        dur_match = re.search(r"\b(?:por\s+)?(\d+(?:\.\d+)?)\s*(?:hora|horas|hr|hrs|h)\b", lower)
        if dur_match:
            val = float(dur_match.group(1))
            if 0.5 <= val <= 12:
                out["duration_hours"] = val

    if out.get("start_time") and out.get("duration_hours"):
        try:
            st_parts = [int(x) for x in out["start_time"].split(":")]
            st_dt = datetime.combine(base_today, time(st_parts[0], st_parts[1]))
            end_dt = st_dt + timedelta(minutes=int(round(float(out["duration_hours"]) * 60)))
            out["end_time"] = end_dt.time().strftime("%H:%M")
        except Exception:
            pass

    # 4. Cantidad de integrantes
    mem_match = re.search(r"\b(\d{1,2})\s*(?:integrantes|m[uú]sicos|mariachis|personas\s+en\s+escena)\b", lower)
    if mem_match:
        out["members_count"] = int(mem_match.group(1))

    # 5. Tipo de evento
    for kw, label in _EVENT_TYPES_KEYWORDS:
        if kw in lower:
            out["event_type"] = label
            break

    # 6. Dirección y ciudad/distrito
    addr_match = re.search(
        r"\b((?:jr\.?|jir[oó]n|av\.?|avenida|calle|psje\.?|pasaje|urb\.?|urbanizaci[oó]n|asoc\.?|mz\.?)\s+[^,.;\n]{4,80})",
        raw,
        flags=re.IGNORECASE,
    )
    if addr_match:
        out["location_address"] = addr_match.group(1).strip(" .,")

    city_match = re.search(
        r"\b(?:distrito\s+de\s+|en\s+)(huamanga|ayacucho|san\s+juan\s+bautista|carmen\s+alto|jes[uú]s\s+nazareno|andahuaylas|huanta|quinua|miraflores|surco|san\s+isidro|la\s+molina|san\s+borja|barranco|lima|callao|cusco|arequipa|trujillo)\b",
        lower,
    )
    if city_match:
        out["location_city"] = city_match.group(1).title()
    elif addr_match:
        # «Jr Lima 450, Comas»: lo que sigue a la dirección tras una coma es el distrito.
        tail = re.match(r"\s*,\s*([A-Za-zÁÉÍÓÚÑáéíóúñ ]{3,40}?)\s*(?:[,.;\n]|$)", raw[addr_match.end(1) :])
        if tail and tail.group(1).split()[0].lower() not in _NOT_A_PLACE_WORDS:
            out["location_city"] = tail.group(1).strip().title()

    # 7. Teléfono (Perú 9 dígitos o internacional)
    phone_match = re.search(r"(?:\+?51\s*)?(9\d{2}[\s-]?\d{3}[\s-]?\d{3})\b", raw)
    if phone_match:
        out["client_phone"] = re.sub(r"[\s-]", "", phone_match.group(1))

    # 8. Correo electrónico
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", raw)
    if email_match:
        out["client_email"] = email_match.group(0).lower()

    # 9. Nombre del cliente
    name_match = re.search(
        r"(?:a\s+nombre\s+de|cliente|para\s+(?:el\s+sr\.?|la\s+sra\.?|don|do[ñn]a)|mi\s+nombre\s+es|soy)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,3})",
        raw,
        flags=re.IGNORECASE,
    )
    if name_match:
        candidate_name = name_match.group(1).strip(" .,")
        stop_words = {"para", "por", "el", "la", "en", "con", "celular", "telefono", "teléfono", "correo", "dni"}
        name_words = []
        for w in candidate_name.split():
            if w.lower() in stop_words:
                break
            name_words.append(w)
        if name_words:
            out["client_fullname"] = " ".join(name_words).title()

    # 10. Precio explícito (ej. "S/ 480", "500 soles", "quedamos en 450")
    price_match = re.search(
        r"(?:s/\s*(\d{2,5}(?:\.\d{1,2})?)|(\d{2,5}(?:\.\d{1,2})?)\s*soles|(?:quedamos\s+en|precio|por)\s+s/?\s*(\d{3,5}(?:\.\d{1,2})?))",
        lower,
    )
    if price_match:
        val_str = price_match.group(1) or price_match.group(2) or price_match.group(3)
        if val_str:
            val_num = float(val_str)
            if 10.0 <= val_num <= 50000.0:
                out["price"] = val_num

    # 11. Detección de regateo / solicitud de descuento
    if any(
        k in lower
        for k in (
            "descuento",
            "rebaja",
            "último precio",
            "ultimo precio",
            "déjame en",
            "dejame en",
            "más cómodo",
            "mas comodo",
        )
    ):
        out["requested_discount"] = True
    # «¿Hay algo menos?» suele pedir un formato más chico, no un descuento: primero se ofrece el paquete más barato.
    elif re.search(
        r"\b(algo menos|menos costos|más barat|mas barat|más económic|mas economic|más accesible|mas accesible"
        r"|otra opci[oó]n m[aá]s|algo m[aá]s econ)",
        lower,
    ):
        out["asked_cheaper"] = True

    return out


def _call_gemini_interaction(
    *,
    system_instruction: str,
    user_content: list[dict[str, Any]] | str,
    response_schema: dict[str, Any] | None = None,
    model: str | None = None,
    usage_db: Session | None = None,
    usage_user_id: Any = None,
    has_audio: bool = False,
) -> dict[str, Any] | None:
    """Llama a Gemini Interactions API (`POST /v1beta/interactions` con `gemini-3.8-flash`).

    Chivapp paga cada llamada, así que antes se descuenta del cupo del músico y del tope global
    (`assistant_usage`). Sin `usage_db` y `usage_user_id` no se llama al modelo (falla cerrado).
    Si el cupo está agotado lanza `AssistantQuotaExceeded`.
    """
    api_key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
    if not api_key:
        return None
    if usage_db is None or usage_user_id is None:
        logger.error("Llamada al modelo sin contexto de cupo: se descarta.")
        return None
    if not consume_ai_call(usage_db, usage_user_id, audio=has_audio):
        raise AssistantQuotaExceeded("audio" if has_audio else "ai")

    target_model = model or getattr(settings, "GEMINI_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash"
    payload: dict[str, Any] = {
        "model": target_model,
        "system_instruction": system_instruction,
        "input": user_content,
        "store": False,
        # Razonamiento bajo: cada token de razonamiento se cobra como salida y aquí no aporta.
        "generation_config": {"thinking_level": "low"},
    }
    if response_schema is not None:
        payload["response_format"] = [
            {
                "type": "json_schema",
                "json_schema": response_schema,
            }
        ]

    try:
        with httpx.Client(timeout=18.0) as client:
            resp = client.post(
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                headers={
                    "x-goog-api-key": api_key,
                    "Api-Revision": "2026-05-20",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                logger.warning("Gemini Interactions API status=%s body=%s", resp.status_code, resp.text[:300])
                return None
            data = resp.json()
            # Extraer el texto del último model_output step
            output_text = data.get("output_text")
            if not output_text:
                steps = data.get("steps") or []
                text_parts: list[str] = []
                for step in steps:
                    if step.get("type") == "model_output":
                        for item in step.get("content") or []:
                            if item.get("type") == "text" and item.get("text"):
                                text_parts.append(item["text"])
                output_text = "\n".join(text_parts).strip()
            return {"output_text": output_text, "raw": data}
    except Exception as exc:
        logger.warning("Fallo al invocar Gemini Interactions API: %s", exc)
        return None


def extract_booking_fields_from_text_or_audio(
    *,
    text: str | None = None,
    audio_base64: str | None = None,
    audio_mime_type: str = "audio/webm",
    usage_db: Session | None = None,
    usage_user_id: Any = None,
) -> dict[str, Any]:
    """Extrae los campos de una reserva/contrata desde un audio o texto dictado por el músico.

    Con audio, si el músico agotó su cupo lanza `AssistantQuotaExceeded` (sin el modelo no se puede transcribir).
    Con solo texto, sin cupo se usa el extractor por reglas.
    """
    today_iso = get_lima_now().date().isoformat()
    clean_b64 = None
    if audio_base64:
        clean_b64 = audio_base64.strip()
        if "," in clean_b64 and clean_b64.startswith("data:"):
            header, clean_b64 = clean_b64.split(",", 1)
            mime_match = re.match(r"data:([^;]+);base64", header)
            if mime_match:
                audio_mime_type = mime_match.group(1)

    schema = {
        "name": "booking_extraction",
        "schema": {
            "type": "object",
            "properties": {
                "transcript": {"type": ["string", "null"]},
                "price": {"type": ["number", "null"]},
                "event_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "start_time": {"type": ["string", "null"], "description": "HH:MM (24h)"},
                "end_time": {"type": ["string", "null"], "description": "HH:MM (24h)"},
                "event_type": {"type": ["string", "null"]},
                "location_address": {"type": ["string", "null"]},
                "location_city": {"type": ["string", "null"]},
                "location_reference": {"type": ["string", "null"]},
                "client_fullname": {"type": ["string", "null"]},
                "client_phone": {"type": ["string", "null"]},
                "client_email": {"type": ["string", "null"]},
                "client_document_number": {"type": ["string", "null"]},
                "event_description": {"type": ["string", "null"]},
            },
        },
    }

    content_items: list[dict[str, Any]] = []
    if clean_b64:
        content_items.append(
            {
                "type": "audio",
                "data": clean_b64,
                "mime_type": audio_mime_type or "audio/webm",
            }
        )
    if text and text.strip():
        content_items.append({"type": "text", "text": text.strip()})
    else:
        content_items.append(
            {
                "type": "text",
                "text": "Transcribe el audio y extrae todos los datos del contrato/evento en JSON.",
            }
        )

    try:
        gemini_res = _call_gemini_interaction(
            system_instruction=(
                f"Hoy en Perú (America/Lima) es {today_iso}. Eres un extractor de datos de reservas musicales para Chivapp. "
                "Extrae del texto o audio del músico los campos del evento en formato JSON válido según el esquema."
            ),
            user_content=[{"type": "user_input", "content": content_items}],
            response_schema=schema,
            usage_db=usage_db,
            usage_user_id=usage_user_id,
            has_audio=bool(clean_b64),
        )
    except AssistantQuotaExceeded:
        if clean_b64:
            raise
        gemini_res = None

    extracted: dict[str, Any] = {}
    if gemini_res and gemini_res.get("output_text"):
        try:
            extracted = json.loads(gemini_res["output_text"])
        except Exception:
            pass

    # Combinar con el extractor heurístico para garantizar cobertura total (especialmente en tests o sin API key)
    source_text = (extracted.get("transcript") or text or "").strip()
    heuristic = _extract_fields_heuristic(source_text)
    merged: dict[str, Any] = {"transcript": extracted.get("transcript") or (text.strip() if text else None)}
    for key in (
        "price",
        "event_date",
        "start_time",
        "end_time",
        "event_type",
        "location_address",
        "location_city",
        "location_reference",
        "client_fullname",
        "client_phone",
        "client_email",
        "client_document_number",
        "event_description",
    ):
        val = extracted.get(key)
        if val in (None, ""):
            val = heuristic.get(key)
        merged[key] = val

    return merged


def should_ignore_incoming_message(
    db: Session,
    *,
    user: User,
    channel: MusicianWhatsAppChannel,
    client_phone: str | None,
    message_text: str,
    is_new_conversation: bool,
) -> tuple[bool, str | None]:
    """Evalúa las 3 reglas de protección para no responder a integrantes ni chats personales."""
    norm_incoming = normalize_phone(client_phone) if client_phone else None

    # 1. Lista manual de teléfonos ignorados
    if norm_incoming:
        ignored_set = {normalize_phone(p) for p in (channel.ignored_phones or []) if p}
        if norm_incoming in ignored_set:
            return True, "Teléfono en lista de exclusión del músico"

    # 2. Integrantes registrados en cualquiera de sus agrupaciones
    if channel.ignore_ensemble_members and norm_incoming:
        members = (
            db.query(EnsembleMember)
            .filter(
                EnsembleMember.leader_user_id == user.id,
                EnsembleMember.status.in_([EnsembleMemberStatus.invited, EnsembleMemberStatus.active]),
            )
            .all()
        )
        for m in members:
            if m.phone and normalize_phone(m.phone) == norm_incoming:
                return True, f"Es integrante de tu agrupación ({m.fullname})"

    # 3. Clasificador de chat personal/familiar en el primer mensaje
    if channel.ignore_personal_chats and is_new_conversation:
        lower = (message_text or "").strip().lower()
        business_keywords = (
            "cotiz",
            "precio",
            "cuanto",
            "cuánto",
            "cobran",
            "cobra",
            "serenata",
            "cumple",
            "evento",
            "mariachi",
            "boda",
            "matrimonio",
            "aniversario",
            "disponib",
            "libre",
            "fecha",
            "show",
            "presentaci",
            "hora",
            "contrato",
            "contrata",
            "cancion",
            "repertorio",
            "tocar",
            "tocan",
            "sábado",
            "sabado",
            "domingo",
            "viernes",
            "quince",
        )
        personal_markers = (
            "mamá",
            "mama",
            "papá",
            "papa",
            "almuerzo",
            "almorzar",
            "cenar",
            "comprar pan",
            "ya llegué",
            "ya llegue",
            "hermano",
            "amor",
            "primo",
            "tío",
            "tia",
            "ensayo",
        )
        has_business = any(k in lower for k in business_keywords)
        has_personal = any(k in lower for k in personal_markers)
        if has_personal and not has_business:
            return True, "Mensaje detectado como conversación personal/familiar"

    return False, None


def resolve_profile_for_conversation(
    db: Session,
    user: User,
    *,
    requested_profile_id=None,
    message_text: str | None = None,
) -> MusicianProfile:
    """Resuelve qué perfil del músico atiende la conversación (soporta múltiples perfiles por persona)."""
    profiles = list_user_profiles(db, user)
    if not profiles:
        raise HTTPException(status_code=400, detail="No tienes perfiles de músico creados.")

    if requested_profile_id:
        for p in profiles:
            if str(p.id) == str(requested_profile_id):
                return p

    published = [p for p in profiles if p.status == ProfileStatus.published]
    candidates = published or profiles

    if message_text and len(candidates) > 1:
        lower = message_text.lower()
        for p in candidates:
            if p.stage_name and p.stage_name.lower() in lower:
                return p
            for g in (p.genres or []):
                if g and g.lower() in lower:
                    return p
            for inst in (p.instruments or []):
                if inst and inst.lower() in lower:
                    return p

    for p in candidates:
        if p.is_primary:
            return p
    return candidates[0]


def _create_or_update_prefilled_booking_link(
    db: Session,
    *,
    profile: MusicianProfile,
    conversation: AssistantConversation,
    extracted: dict[str, Any],
    quote: dict[str, Any],
    source: str,
) -> BookingLink:
    """Crea o actualiza el BookingLink pre-llenado con todos los datos recolectados en la conversación."""
    ev_date = date.fromisoformat(extracted["event_date"])
    st_parts = [int(x) for x in str(extracted["start_time"]).split(":")[:2]]
    st_time = time(st_parts[0], st_parts[1])

    end_time_val: time | None = None
    if extracted.get("end_time"):
        et_parts = [int(x) for x in str(extracted["end_time"]).split(":")[:2]]
        end_time_val = time(et_parts[0], et_parts[1])

    description_lines = [quote.get("summary") or f"Show con {profile.stage_name}"]
    if extracted.get("requested_repertoire"):
        description_lines.append(f"Canciones solicitadas: {extracted['requested_repertoire']}")

    existing_link = (
        db.get(BookingLink, conversation.last_booking_link_id)
        if conversation.last_booking_link_id
        else None
    )
    if existing_link and existing_link.status == BookingLinkStatus.active:
        link = existing_link
        link.price = Decimal(str(quote["final_price"]))
        link.event_date = ev_date
        link.start_time = st_time
        link.end_time = end_time_val
        link.event_type = str(extracted.get("event_type") or "Evento musical")[:120]
        link.location_address = str(extracted.get("location_address") or "")[:300]
        link.location_city = str(extracted.get("location_city") or profile.location_city or "")[:120] or None
        link.location_reference = str(extracted.get("location_reference") or "")[:255] or None
        link.client_fullname = str(extracted.get("client_fullname") or conversation.client_name or "")[:120] or None
        link.client_phone = str(extracted.get("client_phone") or conversation.client_phone or "")[:40] or None
        link.client_email = str(extracted.get("client_email") or "")[:160] or None
        link.event_description = "\n".join(description_lines)
        link.expires_at = datetime.utcnow() + timedelta(hours=24)
    else:
        link = BookingLink(
            token=secrets.token_urlsafe(16),
            musician_id=profile.id,
            price=Decimal(str(quote["final_price"])),
            event_date=ev_date,
            start_time=st_time,
            end_time=end_time_val,
            event_type=str(extracted.get("event_type") or "Evento musical")[:120],
            location_address=str(extracted.get("location_address") or "")[:300],
            location_city=str(extracted.get("location_city") or profile.location_city or "")[:120] or None,
            location_reference=str(extracted.get("location_reference") or "")[:255] or None,
            client_fullname=str(extracted.get("client_fullname") or conversation.client_name or "")[:120] or None,
            client_phone=str(extracted.get("client_phone") or conversation.client_phone or "")[:40] or None,
            client_email=str(extracted.get("client_email") or "")[:160] or None,
            event_description="\n".join(description_lines),
            source=source,
            status=BookingLinkStatus.active,
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )
        db.add(link)
        db.flush()
        conversation.last_booking_link_id = link.id

    return link


_MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "setiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip
_WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

_INCLUDE_LABELS = {
    "sound": "equipo de sonido",
    "lights": "luces",
    "transport": "transporte",
    "outfit": "vestuario de presentación",
    "requests": "pedidos de canciones del cliente",
    "mc": "animador o presentador (MC)",
}
_SETUP_LABELS = {"30m": "unos 30 minutos", "1h": "1 hora", "2h+": "2 horas o más"}
_FORMAT_LABELS = {
    "acoustic": "acústico",
    "electric": "eléctrico",
    "orchestra": "orquesta",
    "mixed": "mixto",
}

_INFO_QUESTION_MARKERS = (
    "?", "explica", "explícame", "cuéntame", "cuentame", "cuenta me", "qué incluye", "que incluye",
    "qué contiene", "que contiene", "contiene", "incluye", "quiénes son", "quienes son", "más de ti",
    "mas de ti", "repertorio", "canciones", "temas", "video", "cómo es", "como es", "cómo funciona",
    "como funciona", "garantía", "garantia", "experiencia",
)  # fmt: skip
_NOT_A_PLACE_WORDS = frozenset(
    {"con", "y", "para", "por", "a", "el", "la", "los", "las", "mi", "me", "soy", "es", "será", "sera", "somos", "quiero", "necesito", "gracias"}
)
_ACK_WORDS = ("gracias", "ok", "okey", "vale", "listo", "perfecto", "genial", "dale", "excelente", "entendido")

# Campos que, al venir en el mensaje del cliente, cambian la reserva (y por tanto el enlace).
_BOOKING_FIELDS = (
    "date_conflict", "event_date", "start_time", "end_time", "event_type", "location_address", "location_city",
    "members_count", "duration_hours", "requested_discount", "asked_cheaper", "client_fullname",
    "client_email",
)  # fmt: skip


def _human_date(iso: Any) -> str:
    """'2026-11-20' -> 'viernes 20 de noviembre'. Si no se entiende, devuelve el texto tal cual."""
    try:
        d = date.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{_WEEKDAY_NAMES[d.weekday()]} {d.day} de {_MONTH_NAMES[d.month - 1]}"


def _has_district(extracted: dict[str, Any], profile: MusicianProfile, config: MusicianAssistantConfig) -> bool:
    """True si se sabe en qué distrito o ciudad es el evento (dicho o contenido en la dirección)."""
    if extracted.get("location_city"):
        return True
    haystack = (extracted.get("location_address") or "").lower()
    details = getattr(profile, "details", None) or {}
    known = [
        getattr(profile, "location_city", None),
        *(details.get("other_cities") or []),
        *[z.get("zone") for z in (config.zone_surcharges or []) if isinstance(z, dict)],
    ]
    return any(k and str(k).strip().lower() in haystack for k in known)


def _date_conflict_message(conflict: dict[str, Any]) -> str:
    """Pregunta cuál fecha quiso decir cuando el día de la semana no coincide con el número."""
    try:
        said = date.fromisoformat(str(conflict.get("date")))
        weekday_idx = _WEEKDAY_MAP[str(conflict.get("weekday")).lower()]
    except (TypeError, ValueError, KeyError):
        return "No me quedó claro el día del evento. ¿Me confirmas la fecha completa?"
    delta = (weekday_idx - said.weekday()) % 7
    if delta > 3:
        delta -= 7
    other = said + timedelta(days=delta)
    month = _MONTH_NAMES[said.month - 1]
    return (
        f"Me dices *{conflict.get('weekday')} {said.day} de {month}*, pero el {said.day} de {month} cae "
        f"{_WEEKDAY_NAMES[said.weekday()]}. ¿Te refieres al *{_human_date(said.isoformat())}* "
        f"o al *{_human_date(other.isoformat())}*?"
    )


def _wa_format(text: str) -> str:
    """WhatsApp pone en negrita con un solo asterisco: '**x**' (Markdown) se vería con los asteriscos a la vista."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)


def _classify_followup(message: str) -> str | None:
    """'info' si el cliente pregunta por el show/el artista, 'ack' si solo agradece o confirma, si no None."""
    lower = (message or "").strip().lower()
    if not lower:
        return None
    if any(marker in lower for marker in _INFO_QUESTION_MARKERS):
        return "info"
    words = re.findall(r"[a-záéíóúñ]+", lower)
    if words and len(words) <= 4 and any(w in _ACK_WORDS for w in words):
        return "ack"
    return None


def _show_facts(profile: MusicianProfile, config: MusicianAssistantConfig, quote: dict[str, Any]) -> list[str]:
    """Datos reales del perfil para contar qué contiene el show (nunca se inventa nada fuera de esto)."""
    facts: list[str] = []
    bio = (getattr(profile, "bio", None) or getattr(profile, "portfolio_description", None) or "").strip()
    if bio:
        facts.append(f"Sobre el artista: {bio[:320]}")
    pkg_name = quote.get("package_name")
    if pkg_name:
        line = f"Formato cotizado: {pkg_name}"
        for pkg in config.packages or []:
            if pkg.get("name") == pkg_name and pkg.get("description"):
                line += f" — {pkg['description']}"
                break
        facts.append(line)
    details = getattr(profile, "details", None) or {}
    includes = [_INCLUDE_LABELS[i] for i in (details.get("includes") or []) if i in _INCLUDE_LABELS]
    if includes:
        facts.append("El show incluye: " + ", ".join(includes))
    if details.get("setup") in _SETUP_LABELS:
        facts.append(f"Montaje previo: {_SETUP_LABELS[details['setup']]}")
    if details.get("format") in _FORMAT_LABELS:
        facts.append(f"Formato musical: {_FORMAT_LABELS[details['format']]}")
    genres = [g for g in (getattr(profile, "genres", None) or []) if g]
    if genres:
        facts.append("Géneros: " + ", ".join(genres))
    titles = [
        str(r.get("title")) for r in (getattr(profile, "repertoire", None) or [])[:8] if isinstance(r, dict) and r.get("title")
    ] or list((getattr(profile, "songs", None) or [])[:8])
    if titles:
        facts.append("Repertorio de ejemplo: " + ", ".join(titles))
    video = getattr(profile, "showreel_video_url", None) or ((getattr(profile, "videos", None) or [None])[0])
    if video:
        facts.append(f"Video de muestra: {video}")
    return facts


def _info_reply(
    *,
    profile: MusicianProfile,
    config: MusicianAssistantConfig,
    user_message: str,
    quote: dict[str, Any],
    booking_link_url: str | None,
    usage_db: Session | None,
    usage_user_id: Any,
) -> str:
    """Responde una pregunta sobre el show con los datos del perfil; con Gemini si hay cupo, si no por reglas."""
    stage_name = profile.stage_name or "nuestra agrupación"
    facts = _show_facts(profile, config, quote)
    footer = (
        f"\n\nCuando quieras asegurar tu fecha, tu enlace sigue disponible:\n👉 {booking_link_url}"
        if booking_link_url
        else ""
    )
    system_prompt = (
        f"Eres el asistente de reservas de '{stage_name}' en Chivapp. Responde la pregunta del cliente "
        "usando SOLO los datos de abajo; si algo no está en los datos, di que el músico lo confirmará, "
        "sin inventar. Máximo 5 líneas, en español cálido y claro. La conversación ya empezó: no saludes de nuevo. "
        "Usa un solo asterisco para negritas. No des un precio distinto al indicado "
        f"({quote['summary']}). No incluyas enlaces.\n"
        f"Instrucciones del músico: {config.custom_instructions or 'Sé amable, claro y conciso.'}\n"
        "Datos:\n- " + "\n- ".join(facts or ["(el perfil aún no tiene más detalles)"])
    )
    try:
        reply = _call_gemini_interaction(
            system_instruction=system_prompt,
            user_content=user_message,
            usage_db=usage_db,
            usage_user_id=usage_user_id,
        )
    except AssistantQuotaExceeded:
        reply = None
    if reply and reply.get("output_text"):
        return reply["output_text"].strip() + footer

    lines = [f"Con gusto te cuento más de *{stage_name}*:"]
    lines.extend(f"• {fact}" for fact in facts)
    if not facts:
        lines.append("• El músico te confirmará los detalles del show por este medio.")
    return "\n".join(lines) + footer


def _build_assistant_reply(**kwargs: Any) -> str:
    """Respuesta del asistente lista para WhatsApp (negritas con un solo asterisco)."""
    return _wa_format(_build_assistant_reply_raw(**kwargs))


def _build_assistant_reply_raw(
    *,
    profile: MusicianProfile,
    config: MusicianAssistantConfig,
    conversation: AssistantConversation,
    user_message: str,
    extracted: dict[str, Any],
    quote: dict[str, Any],
    availability_ok: bool | None,
    availability_reason: str | None,
    booking_link_url: str | None,
    usage_db: Session | None = None,
    usage_user_id: Any = None,
    data_changed: bool = True,
    extra_note: str | None = None,
) -> str:
    """Construye la respuesta del asistente (vía Gemini 3.8 Flash si hay API key, o motor conversacional estructurado).

    `data_changed` es False cuando el mensaje del cliente no trae datos nuevos de la reserva (una pregunta, un
    «gracias»): entonces no se repite la plantilla del enlace, se contesta lo que preguntó.
    """
    stage_name = profile.stage_name or "nuestra agrupación"
    followup = _classify_followup(user_message)
    link_sent_before = any(
        m.get("booking_link_token") for m in (conversation.messages or []) if isinstance(m, dict)
    )
    repertoire_titles = [
        str(r.get("title")) for r in (profile.repertoire or [])[:6] if isinstance(r, dict) and r.get("title")
    ] or list((profile.songs or [])[:6])
    video_url = profile.showreel_video_url or ((profile.videos or [None])[0] if profile.videos else None)

    if extracted.get("date_conflict"):
        return _date_conflict_message(extracted["date_conflict"])

    if quote.get("out_of_coverage"):
        return (
            f"¡Gracias por escribir a **{stage_name}**! Veo que tu evento es fuera de nuestras zonas habituales "
            f"de cobertura ({profile.location_city or 'nuestra ciudad'}). Ya registré todos tus datos y le avisé "
            "directamente al director de la agrupación para que te confirme el costo de traslado en breve."
        )

    if availability_ok is False:
        return (
            f"Te comento que para esa fecha y hora ({_human_date(extracted.get('event_date'))} a las "
            f"{extracted.get('start_time')}) no podemos: {availability_reason} "
            "¿Te gustaría que revisemos otra hora ese mismo día o una fecha cercana?"
        )

    if booking_link_url:
        # El enlace ya se envió y el cliente no cambió nada de la reserva: se contesta lo que dijo, sin repetir la plantilla.
        if link_sent_before and not data_changed:
            if followup == "info":
                return _info_reply(
                    profile=profile,
                    config=config,
                    user_message=user_message,
                    quote=quote,
                    booking_link_url=booking_link_url,
                    usage_db=usage_db,
                    usage_user_id=usage_user_id,
                )
            if followup == "ack":
                return f"¡Con gusto! Tu enlace sigue disponible para cuando quieras asegurar tu fecha:\n👉 {booking_link_url}"
        client_label = extracted.get("client_fullname") or conversation.client_name or ""
        greeting_name = f" {client_label}" if client_label and client_label not in ("Cliente de Prueba", "Cliente") else ""
        note = f"{extra_note}\n\n" if extra_note else ""
        return (
            f"¡Excelente{greeting_name}! {note}Tenemos disponibilidad confirmada en agenda para tu **{extracted.get('event_type')}** "
            f"el **{_human_date(extracted.get('event_date'))} a las {extracted.get('start_time')}** en **{extracted.get('location_address')}**.\n\n"
            f"💰 **Cotización:** {quote['summary']}\n\n"
            "Para formalizar nuestra contrata con todas las garantías, ya dejé cargados todos tus datos en Chivapp. "
            f"Solo ingresa a este enlace para revisar el contrato digital, firmarlo y asegurar tu fecha:\n👉 {booking_link_url}"
        )

    # Un saludo sin datos del evento: se saluda y se piden los datos, sin soltar un precio que nadie preguntó.
    has_event_info = any(
        extracted.get(k)
        for k in ("event_date", "start_time", "event_type", "location_address", "location_city", "members_count", "duration_hours")
    )
    asks_price = any(
        k in (user_message or "").lower()
        for k in ("precio", "cuánto", "cuanto", "cotiz", "costo", "tarifa", "cobran", "cobra")
    )
    if not has_event_info and not asks_price and followup != "info":
        return config.greeting_message or (
            f"¡Hola! Soy el asistente de **{stage_name}**. Cuéntame para qué fecha, hora, lugar y tipo de evento "
            "deseas cotizar."
        )

    # Identificar qué datos faltan para poder generar el contrato en Chivapp
    missing_labels: list[str] = []
    if not extracted.get("event_date"):
        missing_labels.append("la **fecha del evento**")
    if not extracted.get("start_time"):
        missing_labels.append("la **hora de inicio**")
    if not extracted.get("event_type"):
        missing_labels.append("el **tipo de evento** (cumpleaños, serenata, boda, etc.)")
    if not extracted.get("location_address"):
        missing_labels.append("la **dirección exacta y distrito** del evento")
    elif not _has_district(extracted, profile, config):
        missing_labels.append("el **distrito o ciudad** del evento")

    # Intentar enriquecer con Gemini 3.8 Flash si GEMINI_API_KEY está configurada
    system_prompt = (
        f"Eres el asistente oficial de reservas de '{stage_name}' en Chivapp.\n"
        f"- Ciudad base: {profile.location_city or 'Perú'}\n"
        f"- Tipo: {'Agrupación' if profile.profile_type == 'group' else 'Solista'}\n"
        f"- Géneros: {', '.join(profile.genres or [])}\n"
        f"- Repertorio destacado: {', '.join(repertoire_titles) if repertoire_titles else 'Variado a pedido'}\n"
        f"- Video de muestra: {video_url or 'En el perfil de Chivapp'}\n"
        f"- Cotización calculada actual: {quote['summary']}\n"
        f"- Estado de agenda: {'Disponible' if availability_ok else 'Por verificar fecha y hora'}\n"
        f"- Instrucciones del músico: {config.custom_instructions or 'Sé amable, claro y conciso.'}\n"
        f"- Datos ya recopilados: {json.dumps(extracted, ensure_ascii=False)}\n"
        f"- Datos que aún faltan para generar el link de contrato en Chivapp: {', '.join(missing_labels)}\n"
        + (f"- Dato real del show: {'; '.join(_show_facts(profile, config, quote))}\n" if followup == "info" else "")
        + (f"- Aviso para el cliente: {extra_note}\n" if extra_note else "")
        + "\nREGLA DE ORO: Responde en español en máximo 4 líneas, de forma cálida y profesional. "
        "Si el cliente hizo una pregunta, contéstala primero con los datos de arriba, sin inventar nada. "
        "Dale el precio calculado según la cotización actual (no otro) y TERMINA siempre pidiéndole los datos "
        "que faltan para generarle su link de contrato en Chivapp. Usa un solo asterisco para negritas."
    )
    try:
        gemini_reply = _call_gemini_interaction(
            system_instruction=system_prompt,
            user_content=user_message,
            usage_db=usage_db,
            usage_user_id=usage_user_id,
        )
    except AssistantQuotaExceeded:
        # Sin cupo el asistente sigue respondiendo con la cotización calculada por reglas, sin llamar al modelo.
        gemini_reply = None
    if gemini_reply and gemini_reply.get("output_text"):
        return gemini_reply["output_text"].strip()

    # Respuesta estructurada inteligente (sin requerir API externa)
    intro_parts: list[str] = []
    if len(conversation.messages or []) <= 1:
        intro_parts.append(
            config.greeting_message
            or f"¡Hola! Soy el asistente de **{stage_name}**."
        )
    if followup == "info":
        intro_parts.append(
            "\n".join(f"• {fact}" for fact in _show_facts(profile, config, quote))
            or "El músico te confirmará los detalles del show por este medio."
        )
    if extra_note:
        intro_parts.append(extra_note)
    if availability_ok is True and extracted.get("event_date") and extracted.get("start_time"):
        intro_parts.append(
            f"✅ Te confirmo que **sí tenemos libre el {_human_date(extracted['event_date'])} a las {extracted['start_time']}**."
        )
    intro_parts.append(f"💰 Según lo que me comentas, la tarifa es: **{quote['summary']}**.")
    if followup != "info" and repertoire_titles and any(
        k in user_message.lower() for k in ("cancion", "repertorio", "tocan", "temas", "video")
    ):
        intro_parts.append(f"🎵 Tocamos temas como {', '.join(repertoire_titles[:4])} y más.")
    if missing_labels:
        intro_parts.append(
            f"Para generarte ahora mismo tu enlace de contrato oficial en Chivapp con todo listo, por favor indícame {', '.join(missing_labels)}."
        )
    return "\n\n".join(intro_parts)


def _whatsapp_audio_will_be_ignored(db: Session, user: User, client_phone: str | None) -> bool:
    """True si el turno no recibirá respuesta, así que no vale la pena transcribir su audio."""
    wa_channel = get_or_create_whatsapp_channel(db, user)
    if not wa_channel.is_active:
        return True
    norm_phone = (normalize_phone(client_phone) or client_phone) if client_phone else None
    if norm_phone:
        conversation = (
            db.query(AssistantConversation)
            .filter(
                AssistantConversation.user_id == user.id,
                AssistantConversation.channel == "whatsapp",
                AssistantConversation.client_phone == norm_phone,
            )
            .order_by(AssistantConversation.updated_at.desc())
            .first()
        )
        if conversation and conversation.status in ("paused_by_musician", "ignored_personal"):
            return True
    # Reglas por teléfono (lista de exclusión e integrantes): no dependen del texto del mensaje.
    ignore, _reason = should_ignore_incoming_message(
        db,
        user=user,
        channel=wa_channel,
        client_phone=client_phone,
        message_text="",
        is_new_conversation=False,
    )
    return ignore


def process_assistant_turn(
    db: Session,
    *,
    user: User,
    channel_type: str,
    message_text: str | None = None,
    audio_base64: str | None = None,
    audio_mime_type: str = "audio/webm",
    conversation_id=None,
    requested_profile_id=None,
    client_phone: str | None = None,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Procesa un turno de conversación del cliente (en el Simulador o por Webhook de WhatsApp).

    Siempre responde a un mensaje del cliente: el asistente nunca abre una conversación por su cuenta.
    Lanza `AssistantQuotaExceeded("audio")` si hay un audio y el músico no tiene cupo para transcribirlo.
    """
    # 0. En WhatsApp, un audio de un chat que el asistente va a ignorar (canal apagado, chat pausado o ignorado,
    #    integrante o teléfono excluido) no se transcribe: cada transcripción la paga Chivapp.
    if audio_base64 and channel_type == "whatsapp" and _whatsapp_audio_will_be_ignored(db, user, client_phone):
        audio_base64 = None
        if not (message_text or "").strip():
            message_text = "Audio de consulta de evento"

    # 1. Si viene audio, transcribir/extraer primero
    media_type = "audio" if audio_base64 else "text"
    audio_extracted: dict[str, Any] = {}
    if audio_base64:
        audio_extracted = extract_booking_fields_from_text_or_audio(
            text=message_text,
            audio_base64=audio_base64,
            audio_mime_type=audio_mime_type,
            usage_db=db,
            usage_user_id=user.id,
        )
        if not message_text or not message_text.strip():
            message_text = audio_extracted.get("transcript") or "Audio de consulta de evento"

    clean_text = (message_text or "").strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Envía un mensaje de texto o nota de voz.")

    # 2. Buscar o crear la conversación
    conversation: AssistantConversation | None = None
    if conversation_id:
        conversation = (
            db.query(AssistantConversation)
            .filter(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user.id,
            )
            .first()
        )
    elif channel_type == "whatsapp" and client_phone:
        norm_p = normalize_phone(client_phone) or client_phone
        conversation = (
            db.query(AssistantConversation)
            .filter(
                AssistantConversation.user_id == user.id,
                AssistantConversation.channel == "whatsapp",
                AssistantConversation.client_phone == norm_p,
            )
            .order_by(AssistantConversation.updated_at.desc())
            .first()
        )

    is_new_conv = conversation is None
    profile = resolve_profile_for_conversation(
        db,
        user,
        requested_profile_id=requested_profile_id or (conversation.profile_id if conversation else None),
        message_text=clean_text,
    )
    config = get_or_create_assistant_config(db, profile)

    if conversation is None:
        conversation = AssistantConversation(
            user_id=user.id,
            profile_id=profile.id,
            channel=channel_type,
            client_phone=normalize_phone(client_phone) if client_phone else client_phone,
            client_name=client_name or "Cliente",
            status="bot_active",
            extracted_data={},
            messages=[],
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.profile_id = profile.id

    # Registrar el mensaje entrante del cliente
    now_iso = datetime.utcnow().isoformat()
    msgs = list(conversation.messages or [])
    msgs.append(
        {
            "id": uuid.uuid4().hex[:12],
            "sender": "client",
            "text": clean_text,
            "media_type": media_type,
            "created_at": now_iso,
            "booking_link_token": None,
        }
    )
    conversation.messages = msgs
    conversation.last_message_at = datetime.utcnow()

    # 3. En WhatsApp, verificar si el chat debe ser ignorado (personal / integrante) o está pausado
    if channel_type == "whatsapp":
        wa_channel = get_or_create_whatsapp_channel(db, user)
        if not wa_channel.is_active or not config.enabled:
            db.commit()
            return {
                "conversation": conversation,
                "profile": profile,
                "reply": None,
                "quote": None,
                "booking_link": None,
            }
        if conversation.status in ("paused_by_musician", "ignored_personal"):
            db.commit()
            return {
                "conversation": conversation,
                "profile": profile,
                "reply": None,
                "quote": None,
                "booking_link": None,
            }
        ignore, reason = should_ignore_incoming_message(
            db,
            user=user,
            channel=wa_channel,
            client_phone=conversation.client_phone,
            message_text=clean_text,
            is_new_conversation=is_new_conv,
        )
        if ignore:
            conversation.status = "ignored_personal"
            conversation.escalation_reason = reason
            db.commit()
            return {
                "conversation": conversation,
                "profile": profile,
                "reply": None,
                "quote": None,
                "booking_link": None,
            }

    # 3b. Motor con memoria y herramientas (opcional, `ASSISTANT_ENGINE=agent`). Si no hay clave ni cupo, si falla o si
    #     la respuesta no pasa la validación, el turno lo atiende el motor por reglas de más abajo, como siempre.
    if getattr(settings, "ASSISTANT_ENGINE", "rules") == "agent":
        from app.services.assistant_agent import run_agent_turn

        agent_res: dict[str, Any] | None = None
        try:
            agent_res = run_agent_turn(
                db,
                user=user,
                profile=profile,
                config=config,
                conversation=conversation,
                user_message=clean_text,
                channel_type=channel_type,
            )
        except AssistantQuotaExceeded:
            agent_res = None
        except Exception:
            logger.exception("Falló el motor con herramientas del asistente; atiende el motor por reglas.")
            agent_res = None
        if agent_res is not None:
            agent_link = agent_res.get("booking_link")
            msgs = list(conversation.messages or [])
            msgs.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "sender": "assistant",
                    "text": agent_res["reply"],
                    "media_type": "text",
                    "created_at": datetime.utcnow().isoformat(),
                    "booking_link_token": agent_link.token if agent_link else None,
                    "trace": agent_res.get("trace"),
                }
            )
            conversation.messages = msgs
            conversation.last_message_at = datetime.utcnow()
            db.commit()
            db.refresh(conversation)
            return {
                "conversation": conversation,
                "profile": profile,
                "reply": agent_res["reply"],
                "quote": agent_res.get("quote"),
                "booking_link": agent_link,
                "booking_link_url": agent_res.get("booking_link_url"),
            }

    # 4. Actualizar datos extraídos acumulados en la conversación
    current_data = dict(conversation.extracted_data or {})
    newly_extracted = {**_extract_fields_heuristic(clean_text), **{k: v for k, v in audio_extracted.items() if v is not None}}
    data_changed = any(newly_extracted.get(k) not in (None, "") for k in _BOOKING_FIELDS)
    for k, v in newly_extracted.items():
        if v is not None and v != "":
            current_data[k] = v
    asked_cheaper = bool(current_data.pop("asked_cheaper", None))
    if newly_extracted.get("date_conflict"):
        current_data.pop("event_date", None)  # no se adivina: se le pregunta
    elif newly_extracted.get("event_date"):
        current_data.pop("date_conflict", None)
    if (
        current_data.get("location_address")
        and not _has_district(current_data, profile, config)
        and not any(newly_extracted.get(k) for k in _BOOKING_FIELDS)
        and _classify_followup(clean_text) is None
        and len(clean_text.split()) <= 4
        and not re.search(r"\d", clean_text)
    ):
        # El asistente pidió el distrito y el cliente contestó solo con eso («Comas», «en Surco»).
        bare = re.sub(r"^(?:en|distrito de|en el distrito de)\s+", "", clean_text.strip(" .,"), flags=re.IGNORECASE)
        if bare:
            current_data["location_city"] = bare.title()
            data_changed = True
    if conversation.client_phone and not current_data.get("client_phone"):
        current_data["client_phone"] = conversation.client_phone
    if conversation.client_name and conversation.client_name != "Cliente de Prueba" and not current_data.get("client_fullname"):
        current_data["client_fullname"] = conversation.client_name

    # 5. Verificar disponibilidad si ya tenemos fecha y hora
    availability_ok: bool | None = None
    availability_reason: str | None = None
    ev_date_obj: date | None = None
    st_time_obj: time | None = None
    et_time_obj: time | None = None

    if current_data.get("event_date"):
        try:
            ev_date_obj = date.fromisoformat(str(current_data["event_date"]))
        except ValueError:
            ev_date_obj = None
    if current_data.get("start_time"):
        try:
            parts = [int(x) for x in str(current_data["start_time"]).split(":")[:2]]
            st_time_obj = time(parts[0], parts[1])
        except ValueError:
            st_time_obj = None
    if current_data.get("end_time"):
        try:
            parts = [int(x) for x in str(current_data["end_time"]).split(":")[:2]]
            et_time_obj = time(parts[0], parts[1])
        except ValueError:
            et_time_obj = None

    if ev_date_obj and st_time_obj:
        availability_ok, availability_reason = check_profile_availability_summary(
            db,
            profile,
            ev_date_obj,
            st_time_obj,
            et_time_obj,
        )

    # 5b. «¿Hay algo más económico?»: primero se ofrece el paquete inmediatamente más barato; sin uno, se
    #     aplica el descuento máximo que configuró el músico (si lo hay).
    extra_note: str | None = None
    if asked_cheaper:
        packages = list(config.packages or []) or _default_packages_for_profile(profile)
        base_price = calculate_smart_quote(
            profile,
            config,
            members_count=current_data.get("members_count"),
            duration_hours=current_data.get("duration_hours"),
        )["base_price"]
        cheaper = [p for p in packages if float(p.get("price") or 0) < base_price]
        if cheaper:
            option = max(cheaper, key=lambda p: float(p.get("price") or 0))
            current_data["members_count"] = int(option.get("members_count") or 1)
            extra_note = f"Para algo más económico te propongo *{option.get('name')}*."
        elif float(config.max_discount_amount or 0) > 0 or float(config.max_discount_percent or 0) > 0:
            current_data["requested_discount"] = True

    # 6. Calcular cotización inteligente según reglas del perfil
    quote = calculate_smart_quote(
        profile,
        config,
        members_count=current_data.get("members_count"),
        duration_hours=current_data.get("duration_hours"),
        zone_or_city=current_data.get("location_city"),
        address=current_data.get("location_address"),
        start_time=st_time_obj,
        event_date=ev_date_obj,
        requested_discount=bool(current_data.get("requested_discount")),
    )
    current_data["calculated_price"] = quote["final_price"]
    current_data["quote_summary"] = quote["summary"]
    conversation.extracted_data = current_data

    # 7. Si está fuera de cobertura, escalar al músico
    booking_link: BookingLink | None = None
    booking_link_url: str | None = None

    if quote.get("out_of_coverage"):
        conversation.status = "needs_musician"
        conversation.escalation_reason = "Evento fuera de las zonas de cobertura configuradas"
        notify_user(
            db,
            user=user,
            type="assistant_escalation",
            title="Tu asistente requiere tu intervención",
            message=(
                f"Un cliente ({conversation.client_name or conversation.client_phone or 'Chat'}) "
                "consultó por un evento fuera de tus zonas configuradas."
            ),
        )
    elif (
        availability_ok is True
        and ev_date_obj is not None
        and st_time_obj is not None
        and current_data.get("event_type")
        and current_data.get("location_address")
        and _has_district(current_data, profile, config)
    ):
        # ¡Tenemos todos los datos clave y el horario está libre! Generar el BookingLink pre-llenado en Chivapp
        booking_link = _create_or_update_prefilled_booking_link(
            db,
            profile=profile,
            conversation=conversation,
            extracted=current_data,
            quote=quote,
            source="assistant_whatsapp" if channel_type == "whatsapp" else "assistant_simulator",
        )
        frontend_base = (getattr(settings, "FRONTEND_URL", None) or "https://chiv.app").rstrip("/")
        booking_link_url = f"{frontend_base}/pay/{booking_link.token}"
        conversation.status = "link_sent"

    # 8. Generar respuesta del asistente y guardarla en el historial
    reply_text = _build_assistant_reply(
        profile=profile,
        config=config,
        conversation=conversation,
        user_message=clean_text,
        extracted=current_data,
        quote=quote,
        availability_ok=availability_ok,
        availability_reason=availability_reason,
        booking_link_url=booking_link_url,
        usage_db=db,
        usage_user_id=user.id,
        data_changed=data_changed,
        extra_note=extra_note,
    )

    msgs = list(conversation.messages or [])
    msgs.append(
        {
            "id": uuid.uuid4().hex[:12],
            "sender": "assistant",
            "text": reply_text,
            "media_type": "text",
            "created_at": datetime.utcnow().isoformat(),
            "booking_link_token": booking_link.token if booking_link else None,
        }
    )
    conversation.messages = msgs
    conversation.last_message_at = datetime.utcnow()

    db.commit()
    db.refresh(conversation)

    return {
        "conversation": conversation,
        "profile": profile,
        "reply": reply_text,
        "quote": quote,
        "booking_link": booking_link,
        "booking_link_url": booking_link_url,
    }
