"""Entrenamiento del asistente IA por el propio músico.

Reúne lo que el músico puede enseñarle y revisar, sin tocar cómo funciona el motor:

- **Reglas de derivación:** cuándo el asistente deja de responder y le pasa la conversación (evento grande, fecha
  especial, duda que no sabe). Sin configurar, todo se comporta como siempre.
- **Tono y ejemplos de voz:** cómo suena al redactar con el modelo.
- **Conocimiento:** preguntas y respuestas que el músico escribe o aprende de una duda pendiente. Son datos, no
  preguntas exactas: el motor con herramientas las busca por significado y el motor por reglas por palabras clave.
- **Dudas pendientes:** lo que un cliente preguntó y el asistente no supo; el músico responde una vez.
- **Versiones:** copia automática de la configuración cada vez que cambia, con opción de restaurar.
- **Preparación:** qué tan completo está el asistente.
- **Vista previa:** cotizar casos de ejemplo con las reglas que se están editando, antes de guardarlas.

Las condiciones de pago, cancelación y llegada NO se enseñan aquí: salen de la contrata del músico y de la política
de Chivapp, para que nunca se contradigan (`is_contract_topic`).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.musician_assistant import (
    AssistantConfigVersion,
    AssistantConversation,
    AssistantKnowledgeEntry,
    AssistantPendingQuestion,
)

DEFAULT_ESCALATION_RULES: dict[str, Any] = {
    "big_event_on": False,
    "big_event_guests": 100,
    "special_date_on": False,
    "unknown_question_on": True,
}

TONES = ("cercano", "formal", "breve")
TONE_INSTRUCTIONS = {
    "cercano": "Tono cercano y cálido, frases cortas; puedes usar alguna exclamación.",
    "formal": "Trato de usted, formal y respetuoso; sin exclamaciones ni emojis.",
    "breve": "Muy breve y directo: una o dos líneas, sin rodeos.",
}

_STOP = {
    "para", "pero", "como", "esta", "este", "estan", "tienen", "tiene", "hacen", "hace", "pueden", "puede",
    "quiero", "sobre", "donde", "cuando", "cuanto", "cuantos", "ustedes", "nosotros", "tocar", "toca", "tocan",
    "mismo", "hola", "buenas", "gracias", "favor",
}  # fmt: skip
_CONTRACT_TOPIC = re.compile(
    r"anticip|adelanto|pago|pagar|cancel|reembols|devol|reprogram|llegan|llegada|montaje|garantia|contrat"
)


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", (value or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _keywords(value: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9ñ]+", _norm(value)) if len(w) >= 4 and w not in _STOP]


# --- Reglas de derivación, tono y voz ---


def get_escalation_rules(config: Any) -> dict[str, Any]:
    """Reglas vigentes: lo configurado sobre los valores por defecto."""
    rules = dict(DEFAULT_ESCALATION_RULES)
    stored = getattr(config, "escalation_rules", None) or {}
    for key, default in DEFAULT_ESCALATION_RULES.items():
        if key in stored and isinstance(stored[key], type(default)):
            rules[key] = stored[key]
    return rules


def escalation_reason(rules: dict[str, Any], *, guests_count: int | None, event_date: date | None) -> str | None:
    """Motivo para derivar al músico según sus reglas, o None si el asistente puede seguir."""
    from app.services.musician_assistant import _is_special_peru_date

    if rules.get("big_event_on") and guests_count and guests_count > int(rules.get("big_event_guests") or 0):
        return f"Evento grande: {guests_count} personas (tu límite es {int(rules['big_event_guests'])})"
    if rules.get("special_date_on"):
        is_special, label = _is_special_peru_date(event_date)
        if is_special:
            return f"Fecha especial: {label}"
    return None


def tone_prompt(config: Any) -> str:
    """Instrucciones de estilo para el modelo: tono elegido y mensajes de ejemplo del músico."""
    tone = getattr(config, "tone", None) or "cercano"
    lines = [TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["cercano"])]
    examples = [str(e).strip() for e in (getattr(config, "voice_examples", None) or []) if str(e).strip()]
    if examples:
        lines.append(
            "Así escribe el músico (imita su estilo, sin copiar las frases literalmente): "
            + " | ".join(f"«{e[:200]}»" for e in examples[:6])
        )
    return " ".join(lines)


# --- Conocimiento ---


def is_contract_topic(question: str) -> bool:
    """Pago, cancelación, reembolso y llegada los define la contrata y la política de Chivapp, no el músico."""
    return bool(_CONTRACT_TOPIC.search(_norm(question)))


def list_entries(db: Session, profile_id: Any) -> list[AssistantKnowledgeEntry]:
    return (
        db.query(AssistantKnowledgeEntry)
        .filter(AssistantKnowledgeEntry.profile_id == profile_id)
        .order_by(AssistantKnowledgeEntry.created_at)
        .all()
    )


def create_entry(db: Session, profile: Any, question: str, answer: str, source: str = "tu") -> AssistantKnowledgeEntry:
    entry = AssistantKnowledgeEntry(
        profile_id=profile.id, question=question.strip()[:300], answer=answer.strip(), source=source
    )
    db.add(entry)
    db.flush()
    return entry


def match_faq(entries: list[Any], message: str) -> Any | None:
    """Respuesta enseñada que mejor encaja con el mensaje (palabras clave); la usa el motor por reglas."""
    words = set(_keywords(message))
    if not words:
        return None
    best, best_score = None, 0
    for entry in entries:
        hay = set(_keywords(entry.question))
        score = sum(
            1
            for w in words
            if w in hay or (len(w) > 5 and any(len(h) > 5 and h.startswith(w[:5]) for h in hay))
        )
        if score > best_score:
            best, best_score = entry, score
    return best if best_score > 0 else None


# --- Dudas pendientes ---


def register_pending_question(
    db: Session, profile: Any, conversation: Any, question: str, context: str | None = None
) -> AssistantPendingQuestion:
    """Anota una duda que el asistente no supo responder; si ya estaba abierta, solo suma una vez más."""
    clean = " ".join((question or "").split())[:300] or "Duda sin texto"
    for pending in (
        db.query(AssistantPendingQuestion)
        .filter(AssistantPendingQuestion.profile_id == profile.id, AssistantPendingQuestion.status == "open")
        .all()
    ):
        if _norm(pending.question) == _norm(clean):
            pending.asked_count = int(pending.asked_count or 1) + 1
            db.flush()
            return pending
    pending = AssistantPendingQuestion(
        profile_id=profile.id,
        conversation_id=getattr(conversation, "id", None),
        question=clean,
        context=(context or "")[:1000] or None,
        asked_count=1,
        status="open",
    )
    db.add(pending)
    db.flush()
    return pending


def list_open_pending(db: Session, profile_id: Any) -> list[AssistantPendingQuestion]:
    return (
        db.query(AssistantPendingQuestion)
        .filter(AssistantPendingQuestion.profile_id == profile_id, AssistantPendingQuestion.status == "open")
        .order_by(AssistantPendingQuestion.asked_count.desc(), AssistantPendingQuestion.created_at)
        .all()
    )


def answer_pending(db: Session, profile: Any, pending: AssistantPendingQuestion, answer: str) -> AssistantKnowledgeEntry:
    """Convierte la respuesta del músico en conocimiento y cierra la duda."""
    from datetime import datetime

    entry = create_entry(db, profile, pending.question, answer, source="conversacion")
    pending.status = "answered"
    pending.answered_at = datetime.utcnow()
    db.flush()
    return entry


# --- Versiones ---

SNAPSHOT_FIELDS = (
    ("greeting_message", "el saludo"),
    ("custom_instructions", "las instrucciones libres"),
    ("tone", "el tono"),
    ("voice_examples", "los ejemplos de voz"),
    ("packages", "los paquetes"),
    ("additional_hour_price", "la hora adicional"),
    ("zone_surcharges", "las zonas"),
    ("per_km_rate", "la tarifa por kilómetro"),
    ("night_surcharge_amount", "el recargo nocturno"),
    ("night_surcharge_start_hour", "la hora nocturna"),
    ("special_date_surcharge_percent", "el recargo de fecha especial"),
    ("max_discount_amount", "el descuento en soles"),
    ("max_discount_percent", "el descuento en porcentaje"),
    ("escalation_rules", "las reglas de derivación"),
)  # fmt: skip
_NUMERIC_FIELDS = {
    "additional_hour_price",
    "per_km_rate",
    "night_surcharge_amount",
    "special_date_surcharge_percent",
    "max_discount_amount",
    "max_discount_percent",
}


def snapshot_config(config: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for field, _label in SNAPSHOT_FIELDS:
        value = getattr(config, field, None)
        if field in _NUMERIC_FIELDS:
            value = float(value) if value is not None else None
        elif field in {"packages", "zone_surcharges", "voice_examples"}:
            value = list(value or [])
        elif field == "escalation_rules":
            value = dict(value or {})
        snapshot[field] = value
    return snapshot


def _last_version(db: Session, profile_id: Any) -> AssistantConfigVersion | None:
    return (
        db.query(AssistantConfigVersion)
        .filter(AssistantConfigVersion.profile_id == profile_id)
        .order_by(AssistantConfigVersion.number.desc())
        .first()
    )


def record_version(db: Session, profile: Any, config: Any, label: str | None = None) -> AssistantConfigVersion | None:
    """Guarda una versión si la configuración cambió desde la última. None = no hubo cambios."""
    snapshot = snapshot_config(config)
    last = _last_version(db, profile.id)
    if last is not None and last.snapshot == snapshot:
        return None
    if label is None:
        if last is None:
            label = "Versión inicial"
        else:
            changed = [name for field, name in SNAPSHOT_FIELDS if (last.snapshot or {}).get(field) != snapshot.get(field)]
            label = "Cambiaste " + ", ".join(changed[:4]) + ("…" if len(changed) > 4 else "")
    version = AssistantConfigVersion(
        profile_id=profile.id,
        number=(last.number + 1) if last else 1,
        label=label[:255],
        snapshot=snapshot,
    )
    db.add(version)
    db.flush()
    return version


def list_versions(db: Session, profile_id: Any) -> list[AssistantConfigVersion]:
    return (
        db.query(AssistantConfigVersion)
        .filter(AssistantConfigVersion.profile_id == profile_id)
        .order_by(AssistantConfigVersion.number.desc())
        .limit(50)
        .all()
    )


def restore_version(db: Session, profile: Any, config: Any, number: int) -> AssistantConfigVersion | None:
    """Vuelve a una versión anterior y la guarda como una versión nueva (el historial nunca se reescribe)."""
    target = (
        db.query(AssistantConfigVersion)
        .filter(AssistantConfigVersion.profile_id == profile.id, AssistantConfigVersion.number == number)
        .first()
    )
    if target is None:
        return None
    for field, _label in SNAPSHOT_FIELDS:
        if field in (target.snapshot or {}):
            setattr(config, field, target.snapshot[field])
    db.flush()
    return record_version(db, profile, config, label=f"Restauraste la versión {number}") or target


# --- Preparación ---


def compute_readiness(db: Session, profile: Any, config: Any, channel: Any) -> dict[str, Any]:
    entries = db.query(func.count(AssistantKnowledgeEntry.id)).filter(AssistantKnowledgeEntry.profile_id == profile.id).scalar() or 0
    open_pending = (
        db.query(func.count(AssistantPendingQuestion.id))
        .filter(AssistantPendingQuestion.profile_id == profile.id, AssistantPendingQuestion.status == "open")
        .scalar()
        or 0
    )
    tests = (
        db.query(func.count(AssistantConversation.id))
        .filter(AssistantConversation.profile_id == profile.id, AssistantConversation.channel == "simulator")
        .scalar()
        or 0
    )
    packages = len(config.packages or [])
    zones = len(config.zone_surcharges or [])
    voice = len([e for e in (config.voice_examples or []) if str(e).strip()])
    connected = bool(getattr(channel, "phone_number_id", None))

    def item(key: str, title: str, detail: str, progress: float, weight: int) -> dict[str, Any]:
        return {"key": key, "title": title, "detail": detail, "progress": round(max(0.0, min(1.0, progress)), 2), "weight": weight}

    items = [
        item(
            "pricing",
            "Paquetes, zonas y recargos",
            f"{packages} paquetes y {zones} zonas configurados.",
            (0.5 if packages else 0) + (0.5 if zones else 0),
            25,
        ),
        item("knowledge", "Preguntas frecuentes", f"{entries} de 6 respuestas recomendadas.", entries / 6, 25),
        item("voice", "Tu forma de escribir", f"{voice} de 3 mensajes de ejemplo.", voice / 3, 10),
        item(
            "pending",
            "Dudas de clientes sin responder",
            (f"{open_pending} espera tu respuesta." if open_pending == 1 else f"{open_pending} esperan tu respuesta.") if open_pending else "Al día.",
            1 - open_pending / 3,
            20,
        ),
        item(
            "whatsapp",
            "WhatsApp conectado",
            "Conectado." if connected else "Sin conectar. Mientras tanto, prueba en el simulador.",
            1.0 if connected else 0.0,
            10,
        ),
        item("tests", "Pruebas antes de publicar", f"{min(tests, 4)} de 4 conversaciones de prueba.", tests / 4, 10),
    ]
    score = round(sum(i["progress"] * i["weight"] for i in items))
    return {"score": int(score), "items": items}


# --- Vista previa de cotizaciones ---


def config_like(profile: Any, payload: Any | None, saved: Any) -> Any:
    """Objeto con los campos que usa la cotización: lo que se está editando, o lo guardado si no se envía nada."""
    if payload is None:
        return saved
    return SimpleNamespace(
        packages=[p.model_dump() for p in payload.packages],
        additional_hour_price=payload.additional_hour_price,
        zone_surcharges=[z.model_dump() for z in payload.zone_surcharges],
        per_km_rate=payload.per_km_rate,
        night_surcharge_amount=payload.night_surcharge_amount,
        night_surcharge_start_hour=payload.night_surcharge_start_hour,
        special_date_surcharge_percent=payload.special_date_surcharge_percent,
        max_discount_amount=payload.max_discount_amount,
        max_discount_percent=payload.max_discount_percent,
    )


def cheaper_package(packages: list[dict[str, Any]], base_price: float) -> dict[str, Any] | None:
    cheaper = [p for p in packages if float(p.get("price") or 0) < base_price]
    return max(cheaper, key=lambda p: float(p.get("price") or 0)) if cheaper else None


def _parse_time(value: str | None):
    from datetime import time

    if not value:
        return None
    try:
        hours, minutes = value.split(":")[:2]
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def _quote_lines(quote: dict[str, Any]) -> list[dict[str, Any]]:
    lines = [{"label": quote["package_name"], "amount": quote["base_price"]}]
    if quote.get("extra_hours_amount"):
        lines.append({"label": "Horas adicionales", "amount": quote["extra_hours_amount"]})
    if quote.get("zone_surcharge"):
        lines.append({"label": f"Movilidad ({quote.get('matched_zone') or 'zona'})", "amount": quote["zone_surcharge"]})
    if quote.get("night_surcharge"):
        lines.append({"label": "Horario nocturno", "amount": quote["night_surcharge"]})
    if quote.get("special_date_surcharge"):
        lines.append({"label": f"Fecha especial ({quote.get('special_date_label')})", "amount": quote["special_date_surcharge"]})
    if quote.get("discount_applied"):
        lines.append({"label": "Descuento aplicado", "amount": -quote["discount_applied"]})
    return lines


def preview_quote(profile: Any, config: Any, body: Any) -> dict[str, Any]:
    from app.services.musician_assistant import calculate_smart_quote

    special_date = date(date.today().year, 12, 25) if body.special_date else None
    quote = calculate_smart_quote(
        profile,
        config,
        members_count=body.members_count,
        duration_hours=body.duration_hours,
        zone_or_city=body.zone or profile.location_city,
        start_time=_parse_time(body.start_time),
        event_date=special_date,
        requested_discount=body.requested_discount,
    )
    return {
        "total": quote["final_price"],
        "package_name": quote["package_name"],
        "summary": quote["summary"],
        "lines": _quote_lines(quote),
        "out_of_coverage": bool(quote.get("out_of_coverage")),
    }


def _scenarios(profile: Any, config: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Casos típicos cotizados con la configuración dada: (clave, etiqueta, cotización)."""
    from app.services.musician_assistant import calculate_smart_quote

    city = profile.location_city
    year = date.today().year
    base = calculate_smart_quote(profile, config, zone_or_city=city, start_time=_parse_time("20:00"))
    cheaper = cheaper_package(list(config.packages or []), base["base_price"])
    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("standard", "Show estándar de 1 hora", base),
        ("two_hours", "Dos horas de show", calculate_smart_quote(profile, config, duration_hours=2, zone_or_city=city, start_time=_parse_time("20:00"))),
        ("night", "Show nocturno (23:30)", calculate_smart_quote(profile, config, zone_or_city=city, start_time=_parse_time("23:30"))),
        ("special", "Fecha especial (25 de diciembre)", calculate_smart_quote(profile, config, zone_or_city=city, start_time=_parse_time("20:00"), event_date=date(year, 12, 25))),
        ("discount", "El cliente pide descuento", calculate_smart_quote(profile, config, zone_or_city=city, start_time=_parse_time("20:00"), requested_discount=True)),
    ]
    if cheaper:
        cases.append(
            (
                "cheaper",
                "El cliente pide algo más económico",
                calculate_smart_quote(profile, config, members_count=int(cheaper.get("members_count") or 1), zone_or_city=city, start_time=_parse_time("20:00")),
            )
        )
    return cases


def preview_compare(profile: Any, saved: Any, proposed: Any) -> list[dict[str, Any]]:
    current = {key: (label, quote) for key, label, quote in _scenarios(profile, saved)}
    result: list[dict[str, Any]] = []
    for key, label, new_quote in _scenarios(profile, proposed):
        old_label, old_quote = current.get(key, (label, new_quote))
        result.append(
            {
                "key": key,
                "label": label,
                "current_total": old_quote["final_price"],
                "proposed_total": new_quote["final_price"],
                "current_summary": old_quote["summary"],
                "proposed_summary": new_quote["summary"],
                "changed": old_quote["summary"] != new_quote["summary"],
            }
        )
    return result
