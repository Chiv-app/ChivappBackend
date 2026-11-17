"""Validación de lo que escribe el modelo antes de enviarlo al cliente.

El modelo redacta, pero lo que debe ser exacto sale del código: precios, enlaces y condiciones. Antes de enviar una
respuesta se comprueba que:

- todo monto en soles («S/ 450.00») aparezca en lo que devolvieron las herramientas en este turno o sea un precio
  configurado por el músico;
- todo enlace sea uno que generó el sistema (el de pago de esta conversación o uno que vino de las herramientas).

Si algo no coincide, el motor descarta la respuesta del modelo y usa el motor por reglas, que arma el mensaje con
plantillas. Es una red de seguridad: no corrige el texto, lo rechaza.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Iterable

_AMOUNT = re.compile(r"S/\.?\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def _to_number(raw: str) -> float | None:
    value = raw.strip()
    # «1,200.50» o «1.200,50» o «450.00» o «450,5»
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        head, _, tail = value.rpartition(",")
        value = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else value.replace(",", "")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def amounts_in(text: str) -> list[float]:
    found: list[float] = []
    for match in _AMOUNT.finditer(text or ""):
        number = _to_number(match.group(1))
        if number is not None:
            found.append(number)
    return found


def urls_in(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in _URL.findall(text or "")]


def numbers_in(value: Any) -> set[float]:
    """Todos los números que aparecen en una estructura (resultados de herramientas)."""
    found: set[float] = set()
    if isinstance(value, bool):
        return found
    if isinstance(value, (int, float)):
        found.add(round(float(value), 2))
    elif isinstance(value, str):
        found.update(amounts_in(value))
        for token in re.findall(r"\d+(?:[.,]\d+)?", value):
            number = _to_number(token)
            if number is not None:
                found.add(number)
    elif isinstance(value, dict):
        for item in value.values():
            found |= numbers_in(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found |= numbers_in(item)
    return found


def urls_from(value: Any) -> set[str]:
    return set(urls_in(json.dumps(value, ensure_ascii=False, default=str)))


_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "setiembre": 9, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}  # fmt: skip
_WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "sábado": 5, "domingo": 6,
}  # fmt: skip
_WEEKDAY_DATE = re.compile(
    r"\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+(\d{1,2})\s+de\s+"
    r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|setiembre|septiembre|octubre|noviembre|diciembre)\b",
    re.IGNORECASE,
)


def weekday_date_problems(text: str, today: date, allowed_text: str = "") -> list[str]:
    """Fechas con día de la semana que no coinciden («sábado 20 de noviembre» cuando el 20 cae viernes).

    Se permite repetir una fecha tal cual la citó una herramienta: al aclararle al cliente que «sábado 20» no
    coincide, la respuesta legítimamente la menciona.
    """
    problems: list[str] = []
    allowed = (allowed_text or "").lower()
    for match in _WEEKDAY_DATE.finditer(text or ""):
        if match.group(0).lower() in allowed:
            continue
        weekday = _WEEKDAYS[match.group(1).lower()]
        day, month = int(match.group(2)), _MONTHS[match.group(3).lower()]
        candidates: list[date] = []
        for year in (today.year, today.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue
        upcoming = [d for d in candidates if d >= today - timedelta(days=7)]
        target = upcoming[0] if upcoming else (candidates[-1] if candidates else None)
        if target is not None and target.weekday() != weekday:
            problems.append(f"fecha con día de la semana que no coincide: {match.group(0)}")
    return problems


def check_reply(
    reply: str,
    *,
    tool_results: Iterable[Any],
    configured_amounts: Iterable[float] = (),
    allowed_urls: Iterable[str] = (),
    today: date | None = None,
) -> list[str]:
    """Devuelve la lista de problemas de la respuesta; vacía si se puede enviar."""
    tool_results_list = list(tool_results)
    allowed_numbers: set[float] = {round(float(x), 2) for x in configured_amounts}
    allowed_link_set: set[str] = {u.rstrip("/") for u in allowed_urls}
    for result in tool_results_list:
        allowed_numbers |= numbers_in(result)
        allowed_link_set |= {u.rstrip("/") for u in urls_from(result)}

    problems: list[str] = []
    for amount in amounts_in(reply):
        if amount not in allowed_numbers:
            problems.append(f"monto no respaldado por el sistema: S/ {amount:.2f}")
    for url in urls_in(reply):
        if url.rstrip("/") not in allowed_link_set:
            problems.append(f"enlace que no generó el sistema: {url}")
    if today is not None:
        tool_text = json.dumps(list(tool_results_list), ensure_ascii=False, default=str)
        problems.extend(weekday_date_problems(reply, today, tool_text))
    return problems
