"""Motor del asistente con memoria y herramientas (Gemini, Interactions API con function calling).

El modelo conversa con el cliente y, cuando necesita algo exacto, pide una herramienta que ejecuta el código:

- `update_reservation`: valida y guarda los datos de la reserva que dio el cliente.
- `quote`: cotiza con los paquetes, zonas y recargos del músico (el precio nunca lo decide el modelo).
- `check_availability`: revisa la agenda de Chivapp y Google Calendar.
- `search_knowledge`: busca en la contrata, la política de cancelación y el perfil (pgvector).
- `create_booking_link`: crea o actualiza el enlace de pago precargado.
- `handoff_to_musician`: deriva la conversación al músico y le avisa.

Memoria: se envían los últimos mensajes de la conversación más el estado de la reserva. El historial lo guardamos
y recortamos nosotros (`store=false`).

Cupo: el turno completo cuenta como una respuesta del músico; las vueltas adicionales solo cuentan en el tope
global diario. Si el cupo, la clave o el modelo fallan, `run_agent_turn` devuelve None y el motor por reglas
atiende el turno como siempre. Lo mismo si la respuesta no pasa la validación (`assistant_guard`).
"""

from __future__ import annotations

import contextvars
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.booking_link import BookingLink
from app.services import assistant_guard
from app.services.assistant_knowledge import search_knowledge
from app.services.assistant_training import (
    escalation_reason,
    get_escalation_rules,
    register_pending_question,
    tone_prompt,
)
from app.services.assistant_usage import consume_ai_call, consume_extra_call

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6
# Momento (time.monotonic) en que el turno actual debe terminar. Cada petición corre en su propio contexto.
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("assistant_agent_deadline", default=None)
_MAX_CALL_SECONDS = 25.0
_MIN_CALL_SECONDS = 3.0
HISTORY_MESSAGES = 12
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_REQUIRED_FIELDS = ("event_date", "start_time", "event_type", "location_address")

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "update_reservation",
        "description": (
            "Guarda los datos de la reserva que el cliente acaba de dar (solo los que dijo en este mensaje) y los "
            "valida. Devuelve lo guardado, los problemas a aclarar con el cliente y lo que aún falta."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_date": {"type": "string", "description": "Fecha del evento en formato YYYY-MM-DD."},
                "weekday_mentioned": {
                    "type": "string",
                    "description": "Día de la semana que dijo el cliente junto a la fecha (lunes, martes, etc.), si lo dijo.",
                },
                "start_time": {"type": "string", "description": "Hora de inicio en formato 24 h HH:MM."},
                "duration_hours": {"type": "number", "description": "Duración en horas."},
                "event_type": {"type": "string", "description": "Tipo de evento: cumpleaños, boda, serenata, etc."},
                "location_address": {"type": "string", "description": "Calle y número o lugar exacto."},
                "district": {"type": "string", "description": "Distrito o ciudad del evento."},
                "members_count": {"type": "integer", "description": "Cantidad de integrantes que pide el cliente."},
                "guests_count": {"type": "integer", "description": "Cantidad de invitados del evento, si la dijo."},
                "client_fullname": {"type": "string"},
                "client_email": {"type": "string"},
            },
        },
    },
    {
        "type": "function",
        "name": "quote",
        "description": (
            "Calcula el precio con las reglas del músico usando los datos guardados. Es la única fuente de precios: "
            "nunca des un monto que no salga de aquí."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wants_cheaper": {
                    "type": "boolean",
                    "description": "El cliente pide algo más económico: primero se ofrece el paquete más barato.",
                },
                "wants_discount": {"type": "boolean", "description": "El cliente pide expresamente un descuento."},
            },
        },
    },
    {
        "type": "function",
        "name": "check_availability",
        "description": "Revisa si el músico está libre en la fecha y hora guardadas. Requiere fecha y hora.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "search_knowledge",
        "description": (
            "Busca en la contrata del músico, la política de cancelación de Chivapp y su perfil. Úsala para pagos, "
            "cancelaciones, qué incluye el show, repertorio, equipo y llegada. No inventes nada fuera de lo que devuelva."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "La pregunta del cliente, con sus palabras."}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "create_booking_link",
        "description": (
            "Crea (o actualiza) el enlace de pago con todos los datos precargados. Solo funciona si hay fecha, hora, "
            "tipo de evento, dirección y distrito, y el músico está libre. Incluye el enlace tal cual en tu respuesta."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "ask_musician",
        "description": (
            "Anota una duda del cliente que no pudiste responder con `search_knowledge` (algo que el músico debe "
            "contestar una vez para que el asistente lo aprenda). Después dile al cliente que el músico se lo confirmará."
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "La duda del cliente, en una frase."}},
            "required": ["question"],
        },
    },
    {
        "type": "function",
        "name": "handoff_to_musician",
        "description": (
            "Deriva la conversación al músico y le avisa. Úsala cuando el evento sea fuera de cobertura, la fecha sea "
            "especial para él, el cliente pida algo que no sabes o el cliente quiera hablar con una persona."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Motivo corto para el músico."}},
            "required": ["reason"],
        },
    },
]


@dataclass
class AgentTurn:
    """Contexto de un turno: lo que las herramientas leen y modifican."""

    db: Session
    user: Any
    profile: Any
    config: Any
    conversation: Any
    channel_type: str
    state: dict[str, Any]
    user_message: str = ""
    tool_results: list[Any] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    knowledge_used: list[dict[str, Any]] = field(default_factory=list)
    booking_link: Any = None
    booking_link_url: str | None = None
    quote_result: dict[str, Any] | None = None
    handed_off: bool = False
    escalation: str | None = None  # motivo por el que las reglas del músico piden derivar


# --- Herramientas ---


def _missing(turn: AgentTurn) -> list[str]:
    from app.services.musician_assistant import _has_district

    names = {
        "event_date": "fecha del evento",
        "start_time": "hora de inicio",
        "event_type": "tipo de evento",
        "location_address": "dirección exacta",
    }
    missing = [names[f] for f in _REQUIRED_FIELDS if not turn.state.get(f)]
    if turn.state.get("location_address") and not _has_district(turn.state, turn.profile, turn.config):
        missing.append("distrito o ciudad")
    return missing


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> time | None:
    try:
        parts = [int(x) for x in str(value).split(":")[:2]]
        return time(parts[0], parts[1] if len(parts) > 1 else 0)
    except (TypeError, ValueError, IndexError):
        return None


def tool_update_reservation(turn: AgentTurn, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.musician_assistant import (
        _MONTH_NAMES,
        _WEEKDAY_MAP,
        _WEEKDAY_NAMES,
        _human_date,
        get_lima_now,
    )

    problems: list[str] = []
    state = turn.state

    raw_date = args.get("event_date")
    if raw_date:
        parsed = _parse_date(raw_date)
        weekday = str(args.get("weekday_mentioned") or "").strip().lower()
        today = get_lima_now().date()
        if parsed is None:
            problems.append("La fecha no tiene un formato válido; pídele la fecha completa.")
        elif parsed < today:
            problems.append(f"El {_human_date(parsed.isoformat())} ya pasó; pídele una fecha futura.")
        elif weekday and weekday in _WEEKDAY_MAP and parsed.weekday() != _WEEKDAY_MAP[weekday]:
            delta = (_WEEKDAY_MAP[weekday] - parsed.weekday()) % 7
            if delta > 3:
                delta -= 7
            other = parsed + timedelta(days=delta)
            problems.append(
                f"El cliente dijo «{weekday} {parsed.day} de {_MONTH_NAMES[parsed.month - 1]}», pero el {parsed.day} cae "
                f"{_WEEKDAY_NAMES[parsed.weekday()]}. No guardé la fecha: pregúntale si se refiere al "
                f"{_human_date(parsed.isoformat())} o al {_human_date(other.isoformat())}."
            )
            state.pop("event_date", None)
        else:
            state["event_date"] = parsed.isoformat()
            state.pop("date_conflict", None)

    if args.get("start_time"):
        parsed_time = _parse_time(args["start_time"])
        if parsed_time is None:
            problems.append("La hora no tiene un formato válido; pídele la hora de inicio.")
        else:
            state["start_time"] = parsed_time.strftime("%H:%M")

    if args.get("duration_hours") is not None:
        try:
            hours = float(args["duration_hours"])
            if 0.5 <= hours <= 12:
                state["duration_hours"] = hours
            else:
                problems.append("La duración debe estar entre 0,5 y 12 horas.")
        except (TypeError, ValueError):
            problems.append("La duración no es un número válido.")

    if args.get("members_count") is not None:
        try:
            members = int(args["members_count"])
            if 1 <= members <= 60:
                state["members_count"] = members
        except (TypeError, ValueError):
            pass

    if args.get("guests_count") is not None:
        try:
            guests = int(args["guests_count"])
            if 1 <= guests <= 100000:
                state["guests_count"] = guests
        except (TypeError, ValueError):
            pass

    for key_arg, key_state, limit in (
        ("event_type", "event_type", 120),
        ("location_address", "location_address", 300),
        ("district", "location_city", 120),
        ("client_fullname", "client_fullname", 120),
        ("client_email", "client_email", 160),
    ):
        value = str(args.get(key_arg) or "").strip()
        if value:
            state[key_state] = value[:limit]

    if state.get("event_date") and state.get("start_time"):
        # La fecha o la hora cambiaron: la disponibilidad anterior ya no vale.
        state.pop("availability_ok", None)

    result: dict[str, Any] = {
        "saved": {k: state.get(k) for k in (*_REQUIRED_FIELDS, "location_city", "members_count", "duration_hours", "client_fullname") if state.get(k)},
        "problems": problems,
        "missing": _missing(turn),
    }
    # Reglas del músico: algunos eventos (muy grandes, en fechas especiales) los atiende él personalmente.
    turn.escalation = escalation_reason(
        get_escalation_rules(turn.config),
        guests_count=state.get("guests_count"),
        event_date=_parse_date(state.get("event_date")),
    )
    if turn.escalation:
        result["accion"] = f"{turn.escalation}. El músico atiende este evento personalmente: llama a handoff_to_musician."
    return result


def tool_quote(turn: AgentTurn, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.musician_assistant import (
        _default_packages_for_profile,
        calculate_smart_quote,
    )

    state = turn.state
    note: str | None = None
    if args.get("wants_cheaper"):
        packages = list(turn.config.packages or []) or _default_packages_for_profile(turn.profile)
        base = calculate_smart_quote(
            turn.profile, turn.config, members_count=state.get("members_count"), duration_hours=state.get("duration_hours")
        )["base_price"]
        cheaper = [p for p in packages if float(p.get("price") or 0) < base]
        if cheaper:
            option = max(cheaper, key=lambda p: float(p.get("price") or 0))
            state["members_count"] = int(option.get("members_count") or 1)
            note = f"Se ofrece el paquete más económico: {option.get('name')}."
        elif float(turn.config.max_discount_amount or 0) > 0 or float(turn.config.max_discount_percent or 0) > 0:
            state["requested_discount"] = True
            note = "No hay un paquete más económico; se aplicó el descuento máximo del músico."
    if args.get("wants_discount"):
        state["requested_discount"] = True

    ev_date = _parse_date(state.get("event_date"))
    start = _parse_time(state.get("start_time"))
    quote = calculate_smart_quote(
        turn.profile,
        turn.config,
        members_count=state.get("members_count"),
        duration_hours=state.get("duration_hours"),
        zone_or_city=state.get("location_city"),
        address=state.get("location_address"),
        start_time=start,
        event_date=ev_date,
        requested_discount=bool(state.get("requested_discount")),
    )
    state["calculated_price"] = quote["final_price"]
    state["quote_summary"] = quote["summary"]
    turn.quote_result = quote
    result: dict[str, Any] = {
        "total": quote["final_price"],
        "paquete": quote["package_name"],
        "resumen": quote["summary"],
        "fuera_de_cobertura": bool(quote.get("out_of_coverage")),
        "descuento_aplicado": quote.get("discount_applied") or 0,
    }
    if note:
        result["nota"] = note
    if quote.get("out_of_coverage"):
        result["accion"] = "El evento está fuera de las zonas del músico: llama a handoff_to_musician."
    return result


def tool_check_availability(turn: AgentTurn, _args: dict[str, Any]) -> dict[str, Any]:
    from app.services.musician_assistant import check_profile_availability_summary

    ev_date = _parse_date(turn.state.get("event_date"))
    start = _parse_time(turn.state.get("start_time"))
    if not ev_date or not start:
        return {"error": "Faltan la fecha y la hora para revisar la agenda.", "missing": _missing(turn)}
    end = _parse_time(turn.state.get("end_time"))
    ok, reason = check_profile_availability_summary(turn.db, turn.profile, ev_date, start, end)
    turn.state["availability_ok"] = bool(ok)
    return {"disponible": bool(ok), "detalle": reason}


def tool_search_knowledge(turn: AgentTurn, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    found, mode = search_knowledge(turn.db, turn.profile, turn.config, query, k=5)
    turn.knowledge_used.extend({"source": f["source"], "ref": f["ref"], "score": f["score"]} for f in found)
    if mode == "vacio":
        return {"fragmentos": [], "aviso": "No encontré nada sobre eso en la contrata ni en el perfil. No lo inventes."}
    return {"fragmentos": [{"origen": f["source"], "texto": f["content"]} for f in found]}


def tool_create_booking_link(turn: AgentTurn, _args: dict[str, Any]) -> dict[str, Any]:
    from app.services.musician_assistant import (
        _create_or_update_prefilled_booking_link,
        check_profile_availability_summary,
    )

    missing = _missing(turn)
    if missing:
        return {"error": "Todavía faltan datos para crear el enlace.", "missing": missing}
    if turn.escalation:
        return {"error": f"{turn.escalation}: lo atiende el músico; usa handoff_to_musician."}
    if turn.quote_result is None or turn.quote_result.get("out_of_coverage"):
        quote_result = tool_quote(turn, {})
        if quote_result.get("fuera_de_cobertura"):
            return {"error": "El evento está fuera de cobertura: usa handoff_to_musician."}
    ev_date = _parse_date(turn.state.get("event_date"))
    start = _parse_time(turn.state.get("start_time"))
    ok, reason = check_profile_availability_summary(turn.db, turn.profile, ev_date, start, _parse_time(turn.state.get("end_time")))
    turn.state["availability_ok"] = bool(ok)
    if not ok:
        return {"error": f"El músico no está libre en ese horario: {reason}"}
    # El precio del enlace siempre sale de una cotización con los datos actuales.
    quote_result = tool_quote(turn, {})
    link = _create_or_update_prefilled_booking_link(
        turn.db,
        profile=turn.profile,
        conversation=turn.conversation,
        extracted=turn.state,
        quote=turn.quote_result,
        source="assistant_whatsapp" if turn.channel_type == "whatsapp" else "assistant_simulator",
    )
    frontend_base = (getattr(settings, "FRONTEND_URL", None) or "https://chiv.app").rstrip("/")
    turn.booking_link = link
    turn.booking_link_url = f"{frontend_base}/pay/{link.token}"
    turn.conversation.status = "link_sent"
    return {
        "enlace": turn.booking_link_url,
        "total": quote_result["total"],
        "resumen": quote_result["resumen"],
        "vence_en_horas": 24,
    }


def tool_ask_musician(turn: AgentTurn, args: dict[str, Any]) -> dict[str, Any]:
    if not get_escalation_rules(turn.config).get("unknown_question_on"):
        return {"aviso": "El músico no quiere que le anotes dudas: dile con amabilidad que no tienes ese dato."}
    question = str(args.get("question") or "").strip()
    if not question:
        return {"error": "Falta la duda del cliente."}
    pending = register_pending_question(turn.db, turn.profile, turn.conversation, question, turn.user_message)
    return {"ok": True, "aviso": "Anoté la duda para el músico. Dile al cliente que él se lo confirmará por aquí.", "veces": pending.asked_count}


def tool_handoff(turn: AgentTurn, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.booking_notifications import notify_user

    reason = str(args.get("reason") or "El asistente necesita tu ayuda.").strip()[:300]
    turn.conversation.status = "needs_musician"
    turn.conversation.escalation_reason = reason
    notify_user(
        turn.db,
        user=turn.user,
        type="assistant_escalation",
        title="Tu asistente requiere tu intervención",
        message=f"Un cliente ({turn.conversation.client_name or turn.conversation.client_phone or 'Chat'}): {reason}",
    )
    turn.handed_off = True
    return {"ok": True, "aviso": "Ya se le avisó al músico; dile al cliente que le responderá por aquí."}


TOOL_FUNCTIONS = {
    "update_reservation": tool_update_reservation,
    "quote": tool_quote,
    "check_availability": tool_check_availability,
    "search_knowledge": tool_search_knowledge,
    "ask_musician": tool_ask_musician,
    "create_booking_link": tool_create_booking_link,
    "handoff_to_musician": tool_handoff,
}


def execute_tool(turn: AgentTurn, name: str, args: dict[str, Any]) -> dict[str, Any]:
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"error": f"Herramienta desconocida: {name}"}
    try:
        result = function(turn, args if isinstance(args, dict) else {})
    except Exception as exc:  # una herramienta que falla no debe tumbar el turno
        logger.exception("Falló la herramienta %s del asistente", name)
        result = {"error": f"No se pudo ejecutar {name}: {type(exc).__name__}"}
    turn.tools_used.append(name)
    turn.tool_results.append(result)
    return result


# --- Mensaje del sistema e historial ---


def build_system_prompt(turn: AgentTurn) -> str:
    from app.services.musician_assistant import _WEEKDAY_NAMES, _human_date, get_lima_now

    profile, config = turn.profile, turn.config
    today = get_lima_now().date()
    packages = "; ".join(
        f"{p.get('name')} ({p.get('members_count')} integrantes, {p.get('duration_hours')} h)"
        for p in (config.packages or [])
        if isinstance(p, dict) and p.get("name")
    )
    state = {k: v for k, v in turn.state.items() if v not in (None, "") and k not in {"quote_summary"}}
    return (
        f"Eres el asistente de reservas de «{profile.stage_name or 'nuestra agrupación'}» en Chivapp. Atiendes por "
        "WhatsApp a una persona que quiere contratar al músico.\n"
        f"Hoy es {_human_date(today.isoformat())} ({today.isoformat()}, {_WEEKDAY_NAMES[today.weekday()]}). "
        f"Ciudad base: {profile.location_city or 'Perú'}.\n"
        f"Paquetes del músico (los montos solo salen de `quote`): {packages or 'según cotización'}.\n\n"
        "Reglas:\n"
        "1. Nunca inventes precios, fechas, disponibilidad ni condiciones. Los precios salen solo de `quote`, la agenda "
        "de `check_availability`, y el pago, la cancelación, lo que incluye el show y el repertorio de `search_knowledge`. "
        "Si no lo encuentras, llama a `ask_musician` con la duda y dile que el músico se lo confirmará.\n"
        "2. Cuando el cliente dé datos de la reserva, llama a `update_reservation` con lo que dijo en este mensaje. Si "
        "devuelve problemas, acláralos con el cliente antes de seguir.\n"
        "3. Si faltan datos (fecha, hora, tipo de evento, dirección y distrito), pídelos juntos en un solo mensaje corto.\n"
        "4. Con todos los datos, llama en la misma vuelta a `check_availability`, `quote` y `create_booking_link` "
        "(esta última revisa la agenda por su cuenta y falla si el músico no está libre). Copia el enlace tal cual en "
        "tu respuesta. Llama a varias herramientas a la vez cuando no dependan entre sí.\n"
        "5. Si pide algo más económico usa `quote` con wants_cheaper; si pide descuento, con wants_discount.\n"
        "6. Si el evento es fuera de cobertura, si una herramienta te indica que lo atiende el músico, o si pide hablar "
        "con una persona, usa `handoff_to_musician`.\n"
        "7. Responde en español, cálido y breve (máximo 4 líneas). No saludes otra vez si la conversación ya empezó. "
        "Usa un solo asterisco para negritas.\n\n"
        f"Instrucciones del músico: {config.custom_instructions or 'Sé amable, claro y conciso.'}\n"
        f"Estilo: {tone_prompt(config)}\n"
        f"Estado actual de la reserva: {json.dumps(state, ensure_ascii=False, default=str)}"
    )


def build_history(conversation: Any) -> list[dict[str, Any]]:
    """Últimos mensajes de la conversación (sin el actual) como pasos de entrada."""
    messages = [m for m in (conversation.messages or []) if isinstance(m, dict)]
    previous = messages[:-1][-HISTORY_MESSAGES:]
    steps: list[dict[str, Any]] = []
    for message in previous:
        text_value = str(message.get("text") or "").strip()
        if not text_value:
            continue
        if message.get("sender") == "client":
            steps.append({"type": "user_input", "content": text_value})
        elif message.get("sender") == "assistant":
            steps.append({"type": "model_output", "content": [{"type": "text", "text": text_value}]})
    return steps


# --- Llamada al modelo ---


def _interactions_call(system_prompt: str, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    api_key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
    if not api_key:
        return None
    body = {
        "model": settings.GEMINI_MODEL,
        "system_instruction": system_prompt,
        "input": steps,
        "tools": TOOL_DECLARATIONS,
        "store": False,
        "generation_config": {"thinking_level": "low"},
    }
    timeout = _MAX_CALL_SECONDS
    deadline = _DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - _time.monotonic())
        if timeout < _MIN_CALL_SECONDS:
            logger.warning("El turno agotó su tiempo máximo antes de llamar al modelo; atiende el motor por reglas.")
            return None
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                INTERACTIONS_URL,
                headers={"x-goog-api-key": api_key, "Api-Revision": "2026-05-20", "Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as exc:
        logger.warning("Fallo al invocar Gemini (motor con herramientas): %s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning("Gemini (motor con herramientas) status=%s body=%s", resp.status_code, resp.text[:300])
        return None
    return resp.json()


def _final_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for step in response.get("steps") or []:
        if step.get("type") == "model_output":
            for item in step.get("content") or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
    return "\n".join(parts).strip()


def _configured_amounts(config: Any) -> list[float]:
    amounts: list[float] = []
    for package in config.packages or []:
        if isinstance(package, dict) and package.get("price") is not None:
            amounts.append(float(package["price"]))
    for value in (config.additional_hour_price, config.night_surcharge_amount, config.max_discount_amount):
        if value is not None:
            amounts.append(float(value))
    for zone in config.zone_surcharges or []:
        if isinstance(zone, dict) and zone.get("surcharge") is not None:
            amounts.append(float(zone["surcharge"]))
    return amounts


def _lima_today():
    from app.services.musician_assistant import get_lima_now

    return get_lima_now().date()


def run_agent_turn(
    db: Session,
    *,
    user: Any,
    profile: Any,
    config: Any,
    conversation: Any,
    user_message: str,
    channel_type: str,
) -> dict[str, Any] | None:
    """Atiende el turno con el modelo y las herramientas. None = que lo atienda el motor por reglas."""
    if not (getattr(settings, "GEMINI_API_KEY", None) or "").strip():
        return None
    if not consume_ai_call(db, user.id):
        return None  # sin cupo: el motor por reglas sigue cotizando sin el modelo

    turn = AgentTurn(
        db=db,
        user=user,
        profile=profile,
        config=config,
        conversation=conversation,
        channel_type=channel_type,
        state=dict(conversation.extracted_data or {}),
        user_message=user_message,
    )
    system_prompt = build_system_prompt(turn)
    steps: list[dict[str, Any]] = [*build_history(conversation), {"type": "user_input", "content": user_message}]
    started = datetime.utcnow()
    _DEADLINE.set(_time.monotonic() + int(settings.ASSISTANT_AGENT_TIME_BUDGET_SECONDS))
    rounds = 0
    reply = ""
    for _round in range(MAX_ROUNDS):
        if rounds > 0 and not consume_extra_call(db):
            return None
        response = _interactions_call(system_prompt, steps)
        rounds += 1
        if response is None:
            return None
        calls = [s for s in (response.get("steps") or []) if s.get("type") == "function_call"]
        if response.get("status") == "requires_action" and calls:
            steps = steps + list(response.get("steps") or [])
            for call in calls:
                result = execute_tool(turn, str(call.get("name")), call.get("arguments") or {})
                steps.append(
                    {
                        "type": "function_result",
                        "name": call.get("name"),
                        "call_id": call.get("id"),
                        "result": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                    }
                )
            system_prompt = build_system_prompt(turn)
            continue
        reply = _final_text(response)
        break
    if not reply:
        return None
    if not turn.handed_off and ((turn.quote_result or {}).get("out_of_coverage") or turn.escalation):
        # Igual que el motor por reglas: fuera de cobertura o un evento que el músico quiere atender él se deriva
        # siempre, aunque el modelo no lo haya pedido.
        execute_tool(
            turn,
            "handoff_to_musician",
            {"reason": turn.escalation or "Evento fuera de las zonas de cobertura configuradas"},
        )

    allowed_urls = []
    if conversation.last_booking_link_id:
        existing = db.get(BookingLink, conversation.last_booking_link_id)
        if existing is not None:
            frontend_base = (getattr(settings, "FRONTEND_URL", None) or "https://chiv.app").rstrip("/")
            allowed_urls.append(f"{frontend_base}/pay/{existing.token}")
    if turn.booking_link_url:
        allowed_urls.append(turn.booking_link_url)
    problems = assistant_guard.check_reply(
        reply,
        tool_results=turn.tool_results,
        configured_amounts=_configured_amounts(config),
        allowed_urls=allowed_urls,
        today=_lima_today(),
    )
    if turn.booking_link_url and turn.booking_link_url not in reply:
        reply = f"{reply}\n\n👉 {turn.booking_link_url}"
    if problems:
        logger.warning("Respuesta del modelo descartada por el validador: %s", "; ".join(problems))
        conversation.extracted_data = turn.state
        return None

    conversation.extracted_data = turn.state
    trace = {
        "engine": "agent",
        "tools": turn.tools_used,
        "knowledge": turn.knowledge_used[:8],
        "rounds": rounds,
        "ms": int((datetime.utcnow() - started).total_seconds() * 1000),
    }
    return {
        "reply": reply,
        "quote": turn.quote_result,
        "booking_link": turn.booking_link,
        "booking_link_url": turn.booking_link_url,
        "trace": trace,
    }

