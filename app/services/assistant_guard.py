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


def check_reply(
    reply: str,
    *,
    tool_results: Iterable[Any],
    configured_amounts: Iterable[float] = (),
    allowed_urls: Iterable[str] = (),
) -> list[str]:
    """Devuelve la lista de problemas de la respuesta; vacía si se puede enviar."""
    allowed_numbers: set[float] = {round(float(x), 2) for x in configured_amounts}
    allowed_link_set: set[str] = {u.rstrip("/") for u in allowed_urls}
    for result in tool_results:
        allowed_numbers |= numbers_in(result)
        allowed_link_set |= {u.rstrip("/") for u in urls_from(result)}

    problems: list[str] = []
    for amount in amounts_in(reply):
        if amount not in allowed_numbers:
            problems.append(f"monto no respaldado por el sistema: S/ {amount:.2f}")
    for url in urls_in(reply):
        if url.rstrip("/") not in allowed_link_set:
            problems.append(f"enlace que no generó el sistema: {url}")
    return problems
