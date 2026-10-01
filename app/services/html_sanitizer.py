"""Sanitización del HTML que escriben los usuarios (plantillas de contrato)."""

from __future__ import annotations

import nh3

from app.core.config import settings

_TEXT_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "h1", "h2", "h3", "h4",
    "ul", "ol", "li", "blockquote", "hr", "span", "div",
    "table", "thead", "tbody", "tr", "th", "td", "a", "img",
}
_ALIGNABLE = {"p", "h1", "h2", "h3", "h4", "div", "span", "td", "th", "li"}

_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "span": {"data-contract-variable", "class", "style"},
    "td": {"colspan", "rowspan", "style"},
    "th": {"colspan", "rowspan", "style"},
}
for _tag in _ALIGNABLE:
    _ATTRIBUTES.setdefault(_tag, set()).add("style")


def _allowed_image_src(value: str) -> bool:
    """Solo imágenes subidas a Chivapp (mismo origen o nuestro bucket)."""
    if value.startswith("/uploads/") and ".." not in value:
        return True
    if settings.GCP_BUCKET_NAME:
        return value.startswith(f"https://storage.googleapis.com/{settings.GCP_BUCKET_NAME}/")
    return False


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if tag == "img" and attribute == "src":
        return value if _allowed_image_src(value.strip()) else None
    if attribute == "class":
        return "contract-variable" if "contract-variable" in value.split() else None
    return value


def sanitize_contract_html(html: str | None) -> str | None:
    """Deja solo formato de texto, tablas, enlaces http(s)/mailto e imágenes
    subidas a Chivapp. Elimina scripts, iframes, eventos on* y URLs peligrosas."""
    if html is None:
        return None
    return nh3.clean(
        html,
        tags=_TEXT_TAGS,
        attributes=_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        attribute_filter=_attribute_filter,
        filter_style_properties={"text-align", "color", "background-color"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )
