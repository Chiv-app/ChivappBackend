import base64
import re
from datetime import datetime
from html import unescape
from io import BytesIO
from pathlib import Path

from app.core.timezone import now_peru, to_peru_datetime

from xhtml2pdf import pisa

from app.models.booking import Booking
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.user import User
from app.services.uploads import UPLOAD_DIR, ensure_upload_dir

# Coincide con el span de resaltado que genera el editor tipo Word del frontend
# (ver Frontend/src/components/musician/profile-wizard/rich-text-editor.tsx).
VARIABLE_SPAN_PATTERN = re.compile(
    r"<span\b[^>]*data-contract-variable[^>]*>\s*(\{\{[a-z_]+\}\})\s*</span>",
    re.IGNORECASE,
)
PLACEHOLDER_TOKEN_PATTERN = re.compile(r"\{\{[a-z_]+\}\}")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

# Compatibilidad: plantillas guardadas antes del pago único (100 %) pueden usar
# este marcador del antiguo anticipo. Ya no se ofrece, pero se sigue
# renderizando (con el monto total) para que nunca quede el marcador crudo.
LEGACY_ADVANCE_PLACEHOLDER = "{{anticipo}}"

MUSICIAN_BLANK_PLACEHOLDERS = {
    "{{nombre_cliente}}",
    "{{documento_cliente}}",
    "{{tipo_evento}}",
    "{{descripcion_evento}}",
    "{{fecha_evento}}",
    "{{hora_evento}}",
    "{{lugar_evento}}",
    "{{duracion_servicio}}",
    "{{monto_total}}",
    LEGACY_ADVANCE_PLACEHOLDER,
}

CONTRACTOR_BLANK_PLACEHOLDERS = {
    "{{nombre_artista}}",
    "{{correo_artista}}",
    "{{telefono_artista}}",
    "{{tarifa_hora}}",
    "{{tarifa_evento}}",
    "{{tipo_evento}}",
    "{{descripcion_evento}}",
    "{{fecha_evento}}",
    "{{hora_evento}}",
    "{{lugar_evento}}",
    "{{duracion_servicio}}",
    "{{monto_total}}",
    LEGACY_ADVANCE_PLACEHOLDER,
}

# Estilos compartidos con el editor/preview del frontend (ver .contract-doc en
# Frontend/src/app/globals.css) para que el PDF luzca igual a lo escrito.
CONTRACT_PDF_CSS = """
@page { size: A4; margin: 2cm; }
* { box-sizing: border-box; }
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11pt;
    color: #1f2933;
    line-height: 1.6;
}
.contract-body h1 {
    font-size: 18pt;
    font-weight: bold;
    text-align: center;
    color: #111827;
    margin: 0 0 14pt 0;
}
.contract-body h2 {
    font-size: 13pt;
    font-weight: bold;
    text-transform: uppercase;
    color: #111827;
    margin: 14pt 0 6pt 0;
}
.contract-body h3 {
    font-size: 11.5pt;
    font-weight: bold;
    text-transform: uppercase;
    color: #111827;
    margin: 12pt 0 5pt 0;
}
.contract-body p { margin: 0 0 8pt 0; text-align: justify; color: #374151; }
.contract-body ul, .contract-body ol { margin: 0 0 8pt 0; padding-left: 18pt; color: #374151; }
.contract-body li { margin-bottom: 3pt; }
.contract-body blockquote {
    margin: 8pt 0;
    padding: 6pt 10pt;
    border-left: 3pt solid #d1d5db;
    color: #4b5563;
}
.contract-body strong { font-weight: bold; }
.contract-body em { font-style: italic; }
.contract-body u { text-decoration: underline; }
.contract-body s { text-decoration: line-through; }
.contract-body hr { border: none; border-top: 1pt solid #d1d5db; margin: 12pt 0; }
.contract-body img { max-width: 100%; }
.acceptance-block { margin-top: 14pt; font-size: 10pt; color: #4b5563; }
table.signature-table { width: 100%; margin-top: 30pt; border-collapse: collapse; }
td.signature-cell { width: 50%; font-size: 11pt; vertical-align: top; padding-right: 12pt; }
.signature-label { font-weight: bold; }
.signature-spacer { height: 34pt; }
.signature-image { height: 44pt; }
.signature-line { border-top: 1pt solid #9ca3af; padding-top: 4pt; margin-top: 4pt; }
"""

_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _format_price(value) -> str | None:
    if value is None:
        return None
    return f"S/ {float(value):,.2f}"


def _build_musician_context(profile: MusicianProfile, user: User) -> dict[str, str]:
    contract_date = now_peru().strftime("%d/%m/%Y")
    return {
        "{{nombre_artista}}": profile.stage_name or user.fullname,
        "{{correo_artista}}": user.email,
        "{{telefono_artista}}": user.phone or "—",
        "{{tarifa_hora}}": _format_price(profile.price_per_hour) or "—",
        "{{tarifa_evento}}": _format_price(profile.price_per_event) or "—",
        "{{fecha_contrato}}": contract_date,
    }


def _build_contractor_context(profile: ContractorProfile, user: User) -> dict[str, str]:
    contract_date = now_peru().strftime("%d/%m/%Y")
    return {
        "{{nombre_cliente}}": user.fullname,
        "{{documento_cliente}}": profile.document_number or "—",
        "{{tipo_documento_cliente}}": profile.document_type or "—",
        "{{direccion_cliente}}": profile.address or "—",
        "{{ciudad_cliente}}": profile.city or "—",
        "{{correo_cliente}}": user.email,
        "{{telefono_cliente}}": user.phone or "—",
        "{{fecha_contrato}}": contract_date,
    }


def _unwrap_variable_spans(value: str) -> str:
    """Quita el span de resaltado de variables, dejando el token `{{clave}}`."""
    return VARIABLE_SPAN_PATTERN.sub(r"\1", value)


def _replace_placeholders(
    text: str,
    context: dict[str, str],
    blank_placeholders: set[str] | None = None,
) -> str:
    blanks = blank_placeholders or set()
    unwrapped = _unwrap_variable_spans(text)

    def replacer(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in context:
            return context[token]
        if token in blanks:
            return "________________________"
        return token

    return PLACEHOLDER_TOKEN_PATTERN.sub(replacer, unwrapped)


def contract_html_text_length(value: str | None) -> int:
    """Longitud del texto real (sin etiquetas HTML); útil para validar contenido mínimo."""
    if not value:
        return 0
    text = HTML_TAG_PATTERN.sub(" ", value)
    text = unescape(text)
    return len(" ".join(text.split()))


def _resolve_upload_path(upload_url: str | None) -> Path | None:
    if not upload_url:
        return None
    filename = Path(upload_url).name
    if not filename or filename in {".", ".."}:
        return None
    candidate = UPLOAD_DIR / filename
    if candidate.is_file():
        return candidate
    return None


def _image_data_uri(path: Path) -> str | None:
    mime = _IMAGE_MIME_TYPES.get(path.suffix.lower())
    if not mime:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


IMG_TAG_PATTERN = re.compile(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'][^>]*>)', re.IGNORECASE)


def _inline_local_images(html: str) -> str:
    """Convierte imágenes/logos subidos por el músico (`/uploads/...`) a data URIs
    para que xhtml2pdf pueda incrustarlas sin depender de un servidor accesible."""

    def replacer(match: re.Match[str]) -> str:
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        if src.startswith("data:"):
            return match.group(0)
        path = _resolve_upload_path(src)
        if path is None:
            return match.group(0)
        data_uri = _image_data_uri(path)
        if not data_uri:
            return match.group(0)
        return f"{prefix}{data_uri}{suffix}"

    return IMG_TAG_PATTERN.sub(replacer, html)


def _signature_block_html(data_uri: str | None) -> str:
    if data_uri:
        return f'<img class="signature-image" src="{data_uri}" />'
    return '<div class="signature-spacer"></div>'


def _render_html_document(
    *,
    title: str,
    body_html: str,
    left_label: str,
    left_name: str,
    right_label: str,
    right_name: str,
    left_signature_data_uri: str | None = None,
    right_signature_data_uri: str | None = None,
    acceptance_lines: list[str] | None = None,
) -> str:
    acceptance_html = ""
    if acceptance_lines:
        acceptance_html = '<div class="acceptance-block">' + "".join(
            f"<p>{line}</p>" for line in acceptance_lines
        ) + "</div>"
    left_signature_html = _signature_block_html(left_signature_data_uri)
    right_signature_html = _signature_block_html(right_signature_data_uri)
    body_with_images = _inline_local_images(body_html)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>{title}</title>
<style>{CONTRACT_PDF_CSS}</style>
</head>
<body>
<div class="contract-body">{body_with_images}</div>
{acceptance_html}
<table class="signature-table">
<tr>
<td class="signature-cell">
<div class="signature-label">{left_label}</div>
{left_signature_html}
<div class="signature-line">{left_name}</div>
</td>
<td class="signature-cell">
<div class="signature-label">{right_label}</div>
{right_signature_html}
<div class="signature-line">{right_name}</div>
</td>
</tr>
</table>
</body>
</html>"""


def _pdf_bytes_from_html(html: str) -> bytes:
    buffer = BytesIO()
    result = pisa.CreatePDF(html, dest=buffer, encoding="utf-8")
    if result.err:
        raise RuntimeError("No se pudo generar el PDF del contrato")
    return buffer.getvalue()


def _write_pdf_bytes(filename_prefix: str, pdf_bytes: bytes) -> str:
    ensure_upload_dir()
    filename = f"{filename_prefix}-{now_peru().strftime('%Y%m%d%H%M%S')}.pdf"
    destination = ensure_upload_dir() / filename
    destination.write_bytes(pdf_bytes)
    return f"/uploads/{filename}"


def _build_booking_context(
    booking: Booking,
    profile: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
) -> dict[str, str]:
    context = _build_musician_context(profile, musician_user)
    location_parts = [booking.location_address]
    if booking.location_city:
        location_parts.append(booking.location_city)
    if booking.location_reference:
        location_parts.append(booking.location_reference)

    duration = "—"
    if booking.end_time:
        duration = (
            f"{booking.start_time.strftime('%H:%M')} - "
            f"{booking.end_time.strftime('%H:%M')}"
        )

    context.update(
        {
            "{{nombre_cliente}}": contractor_user.fullname,
            "{{documento_cliente}}": contractor.document_number or "—",
            "{{tipo_documento_cliente}}": contractor.document_type or "—",
            "{{direccion_cliente}}": contractor.address or "—",
            "{{ciudad_cliente}}": contractor.city or "—",
            "{{correo_cliente}}": contractor_user.email,
            "{{telefono_cliente}}": contractor_user.phone or "—",
            "{{fecha_evento}}": booking.event_date.strftime("%d/%m/%Y"),
            "{{hora_evento}}": booking.start_time.strftime("%H:%M"),
            "{{lugar_evento}}": ", ".join(location_parts),
            "{{duracion_servicio}}": duration,
            "{{tipo_evento}}": booking.event_type,
            "{{descripcion_evento}}": booking.event_description or "—",
            "{{monto_total}}": _format_price(booking.price_agreed) or "—",
        }
    )
    # Plantillas antiguas: el "anticipo" ahora es el pago único del total.
    context[LEGACY_ADVANCE_PLACEHOLDER] = context["{{monto_total}}"]
    return context


def _booking_contract_content(
    booking: Booking,
    profile: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
) -> tuple[str, str, dict[str, str]]:
    if contract_html_text_length(profile.contract_template_body) < 50:
        raise ValueError("El músico debe tener una plantilla de contrato configurada")

    raw_title = profile.contract_template_title or f"Contrato de servicios - {profile.stage_name}"
    raw_body = profile.contract_template_body
    context = _build_booking_context(
        booking,
        profile,
        musician_user,
        contractor,
        contractor_user,
    )
    all_blanks = MUSICIAN_BLANK_PLACEHOLDERS | CONTRACTOR_BLANK_PLACEHOLDERS
    title = _replace_placeholders(raw_title, context, all_blanks)
    body = _replace_placeholders(raw_body, context, all_blanks)
    return title, body, context


def build_booking_contract_snapshot(
    booking: Booking,
    profile: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
) -> tuple[str, str, dict[str, str]]:
    """Renderiza y congela título/cuerpo/contexto del contrato para la solicitud."""
    return _booking_contract_content(
        booking,
        profile,
        musician_user,
        contractor,
        contractor_user,
    )


def _resolve_signature_data_uri(
    upload_url: str | None,
    *,
    required: bool,
    missing_message: str,
) -> str | None:
    if not upload_url:
        if required:
            raise ValueError(missing_message)
        return None
    signature_path = _resolve_upload_path(upload_url)
    if signature_path is None:
        if required:
            raise ValueError(missing_message)
        return None
    return _image_data_uri(signature_path)


def render_contract_pdf_bytes(
    *,
    title: str,
    body: str,
    context: dict[str, str] | None,
    signature_image_url: str | None = None,
    musician_signature_url: str | None = None,
    signed_at: datetime | None = None,
    sign_ip: str | None = None,
) -> bytes:
    """Genera un PDF en memoria a partir del snapshot persistido (sin guardar archivo)."""
    ctx = context or {}
    right_signature_data_uri: str | None = None
    left_signature_data_uri: str | None = None
    acceptance_lines: list[str] | None = None
    pdf_title = title

    left_signature_data_uri = _resolve_signature_data_uri(
        musician_signature_url,
        required=False,
        missing_message="No se encontró la imagen de firma del músico",
    )

    if signature_image_url:
        right_signature_data_uri = _resolve_signature_data_uri(
            signature_image_url,
            required=True,
            missing_message="No se encontró la imagen de firma del contratista",
        )
        if signed_at is not None:
            peru_signed_at = to_peru_datetime(signed_at)
            signed_at_label = (
                peru_signed_at.strftime("%d/%m/%Y %H:%M (Hora Perú)")
                if peru_signed_at
                else signed_at.strftime("%d/%m/%Y %H:%M")
            )
            acceptance_lines = [
                "<b>Aceptación de términos y condiciones</b>",
                (
                    f"El cliente aceptó digitalmente este contrato el {signed_at_label}"
                    + (f" (IP: {sign_ip})." if sign_ip else ".")
                ),
            ]
        pdf_title = f"{title} (firmado)"

    html = _render_html_document(
        title=pdf_title,
        body_html=body,
        left_label="EL ARTISTA",
        left_name=ctx.get("{{nombre_artista}}", "—"),
        right_label="EL CLIENTE",
        right_name=ctx.get("{{nombre_cliente}}", "—"),
        left_signature_data_uri=left_signature_data_uri,
        right_signature_data_uri=right_signature_data_uri,
        acceptance_lines=acceptance_lines,
    )
    return _pdf_bytes_from_html(html)


def generate_booking_musician_contract_pdf(
    booking: Booking,
    profile: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
) -> str:
    """Legacy: escribe PDF a disco. Preferir build_booking_contract_snapshot."""
    title, body, context = build_booking_contract_snapshot(
        booking,
        profile,
        musician_user,
        contractor,
        contractor_user,
    )
    pdf_bytes = render_contract_pdf_bytes(
        title=title,
        body=body,
        context=context,
        musician_signature_url=getattr(profile, "signature_image_url", None),
    )
    return _write_pdf_bytes(f"contract-booking-{booking.id}", pdf_bytes)


def generate_signed_booking_contract_pdf(
    booking: Booking,
    profile: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
    *,
    signature_image_url: str,
    signed_at: datetime,
    sign_ip: str,
) -> str:
    """Legacy: escribe PDF firmado a disco. Preferir render_contract_pdf_bytes."""
    title, body, context = build_booking_contract_snapshot(
        booking,
        profile,
        musician_user,
        contractor,
        contractor_user,
    )
    pdf_bytes = render_contract_pdf_bytes(
        title=title,
        body=body,
        context=context,
        signature_image_url=signature_image_url,
        musician_signature_url=getattr(profile, "signature_image_url", None),
        signed_at=signed_at,
        sign_ip=sign_ip,
    )
    return _write_pdf_bytes(f"contract-signed-{booking.id}", pdf_bytes)


def generate_musician_contract_pdf(profile: MusicianProfile, user: User) -> str:
    raw_title = profile.contract_template_title or f"Contrato de servicios - {profile.stage_name}"
    raw_body = profile.contract_template_body or (
        f"<p>Contrato estándar de prestación de servicios musicales para {profile.stage_name}.</p>"
    )
    context = _build_musician_context(profile, user)
    title = _replace_placeholders(raw_title, context, MUSICIAN_BLANK_PLACEHOLDERS)
    body = _replace_placeholders(raw_body, context, MUSICIAN_BLANK_PLACEHOLDERS)
    left_signature = _resolve_signature_data_uri(
        profile.signature_image_url,
        required=False,
        missing_message="No se encontró la imagen de firma del músico",
    )

    html = _render_html_document(
        title=title,
        body_html=body,
        left_label="EL ARTISTA",
        left_name=context["{{nombre_artista}}"],
        right_label="EL CLIENTE",
        right_name="Nombre, documento y firma",
        left_signature_data_uri=left_signature,
    )
    pdf_bytes = _pdf_bytes_from_html(html)
    return _write_pdf_bytes(f"contract-musician-{profile.id}", pdf_bytes)


def generate_contractor_contract_pdf(profile: ContractorProfile, user: User) -> str:
    raise NotImplementedError(
        "Solo los músicos pueden generar plantillas PDF de contrato"
    )
