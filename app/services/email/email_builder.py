from __future__ import annotations

from app.services.email.logo import LOGO_CID, LOGO_PUBLIC_URL

APP_NAME = "Chivapp"
APP_URL = "https://chiv.app"
APP_LOGO_URL = LOGO_PUBLIC_URL
APP_LOGO_SRC = APP_LOGO_URL


def build_email_layout(
    *,
    category_badge: str,
    badge_bg: str = "#EEF2FF",
    badge_color: str = "#4338CA",
    title: str,
    greeting: str,
    lead_text: str,
    body_extra_html: str = "",
    info_items: list[tuple[str, str]] | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
    secondary_note: str | None = None,
) -> str:
    """Construye un correo HTML moderno, responsivo y profesional siguiendo las

    mejores prácticas de diseño UI/UX para clientes de correo (Gmail, Apple Mail, Outlook).
    """

    info_card_html = ""
    if info_items:
        rows_html = []
        for index, (label, value) in enumerate(info_items):
            border_top = "border-top: 1px solid #E2E8F0;" if index > 0 else ""
            rows_html.append(f"""
            <tr>
              <td style="padding: 10px 14px; {border_top}">
                <span style="display: block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #64748B; margin-bottom: 2px;">
                  {label}
                </span>
                <span style="display: block; font-size: 15px; font-weight: 600; color: #0F172A; line-height: 1.4;">
                  {value}
                </span>
              </td>
            </tr>
            """)
        joined_rows = "\n".join(rows_html)
        info_card_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 22px 0; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 12px; overflow: hidden;">
          {joined_rows}
        </table>
        """

    cta_html = ""
    if cta_label and cta_url:
        cta_html = f"""
        <div style="margin: 28px 0 16px; text-align: center;">
          <a href="{cta_url}" target="_blank" style="display: inline-block; background-color: #0F172A; color: #FFFFFF !important; font-size: 15px; font-weight: 600; text-decoration: none; padding: 14px 32px; border-radius: 10px; box-shadow: 0 4px 6px -1px rgba(15, 23, 42, 0.15); line-height: 1.2;">
            {cta_label}
          </a>
        </div>
        """

    secondary_note_html = ""
    if secondary_note:
        secondary_note_html = f"""
        <p style="font-size: 13px; color: #64748B; text-align: center; margin: 12px 0 0; line-height: 1.5;">
          {secondary_note}
        </p>
        """

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #F1F5F9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #F1F5F9; padding: 32px 12px;">
    <tr>
      <td align="center">
        <!-- Main Container Card -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width: 580px; background-color: #FFFFFF; border-radius: 16px; border: 1px solid #E2E8F0; overflow: hidden; box-shadow: 0 4px 12px -2px rgba(15, 23, 42, 0.06);">
          
          <!-- Brand Header -->
          <tr>
            <td style="padding: 24px 32px; border-bottom: 1px solid #F1F5F9; background-color: #FFFFFF;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="left" valign="middle">
                    <a href="{APP_URL}" target="_blank" style="text-decoration: none; display: inline-block;">
                      <img src="{APP_LOGO_SRC}" alt="{APP_NAME}" width="107" height="34" style="display: block; width: 107px; height: 34px; max-height: 34px; border: 0; outline: none; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 20px; font-weight: 800; color: #0F172A;" />
                    </a>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; background-color: {badge_bg}; color: {badge_color}; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; padding: 5px 12px; border-radius: 9999px;">
                      {category_badge}
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Email Content Body -->
          <tr>
            <td style="padding: 32px 32px 28px;">
              <h1 style="font-size: 23px; font-weight: 700; color: #0F172A; line-height: 1.3; margin: 0 0 16px 0;">
                {title}
              </h1>
              <p style="font-size: 15px; color: #334155; line-height: 1.6; margin: 0 0 14px 0;">
                {greeting}
              </p>
              <p style="font-size: 15px; color: #334155; line-height: 1.6; margin: 0 0 16px 0;">
                {lead_text}
              </p>

              {info_card_html}

              {body_extra_html}

              {cta_html}

              {secondary_note_html}
            </td>
          </tr>

          <!-- Professional Footer -->
          <tr>
            <td style="padding: 24px 32px; background-color: #F8FAFC; border-top: 1px solid #E2E8F0; text-align: center;">
              <p style="font-size: 14px; font-weight: 700; color: #334155; margin: 0 0 6px 0; letter-spacing: -0.2px;">
                {APP_NAME}
              </p>
              <p style="font-size: 12px; color: #94A3B8; margin: 0 0 12px 0; line-height: 1.5;">
                Este es un correo transaccional generado automáticamente. Si no realizaste esta acción o no reconoces esta cuenta, puedes ignorar este mensaje con seguridad.
              </p>
              <p style="font-size: 11px; color: #94A3B8; margin: 0;">
                Horario de referencia: Lima, Perú (GMT-5) · © 2026 {APP_NAME}
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

