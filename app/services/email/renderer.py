from __future__ import annotations

import html
import re

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def render_template(template: str, context: dict[str, str], *, escape_html: bool = False) -> str:
    """Reemplaza {{clave}}. Con escape_html=True los valores (nombres, mensajes,
    URLs) se escapan: son datos de usuarios y no deben inyectar HTML en correos."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        raw_val = context.get(key)
        value = "" if raw_val is None else str(raw_val)
        return html.escape(value, quote=True) if escape_html else value

    return _PLACEHOLDER.sub(replace, template)
