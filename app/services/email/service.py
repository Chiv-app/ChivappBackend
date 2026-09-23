from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.email_log import EmailLog
from app.models.email_template import EmailTemplate
from app.services.email.client import EmailDeliveryError, send_via_brevo
from app.services.email.defaults import EMAIL_TEMPLATE_DEFAULTS
from app.services.email.renderer import render_template
from app.services.uniqueness import normalize_email


def ensure_email_templates(db: Session, update_existing: bool = True) -> None:
    """Inserta las plantillas por defecto o actualiza el diseño de las existentes."""
    for item in EMAIL_TEMPLATE_DEFAULTS:
        existing = (
            db.query(EmailTemplate)
            .filter(EmailTemplate.slug == item["slug"])
            .first()
        )
        if not existing:
            db.add(
                EmailTemplate(
                    slug=item["slug"],
                    name=item["name"],
                    description=item["description"],
                    subject=item["subject"],
                    html_body=item["html_body"],
                    text_body=item["text_body"],
                    available_variables=item["available_variables"],
                    enabled=True,
                )
            )
        elif update_existing:
            existing.name = item["name"]
            existing.description = item["description"]
            existing.subject = item["subject"]
            existing.html_body = item["html_body"]
            existing.text_body = item["text_body"]
            existing.available_variables = item["available_variables"]
            existing.updated_at = datetime.utcnow()
    db.commit()


def get_template(db: Session, slug: str) -> EmailTemplate | None:
    template = db.query(EmailTemplate).filter(EmailTemplate.slug == slug).first()
    if not template:
        for item in EMAIL_TEMPLATE_DEFAULTS:
            if item["slug"] == slug:
                template = EmailTemplate(
                    slug=item["slug"],
                    name=item["name"],
                    description=item["description"],
                    subject=item["subject"],
                    html_body=item["html_body"],
                    text_body=item["text_body"],
                    available_variables=item["available_variables"],
                    enabled=True,
                )
                db.add(template)
                db.commit()
                db.refresh(template)
                break
    return template


def render_email_template(
    template: EmailTemplate,
    *,
    context: dict[str, str],
    subject: str | None = None,
    html_body: str | None = None,
    text_body: str | None = None,
) -> dict[str, str]:
    return {
        "subject": render_template(subject or template.subject, context),
        "html": render_template(html_body or template.html_body, context),
        "text": render_template(text_body or template.text_body, context),
    }


def send_templated_email(
    db: Session,
    *,
    slug: str,
    to: str,
    context: dict[str, str],
    user_id: UUID | None = None,
    meta: dict | None = None,
    recipient_name: str | None = None,
    extra_attachments: list[dict] | None = None,
) -> EmailLog:
    recipient = normalize_email(to) or to.strip().lower()
    template = get_template(db, slug)

    if not template:
        log = EmailLog(
            template_slug=slug,
            recipient=recipient,
            subject=f"[missing:{slug}]",
            status="failed",
            error_message="Plantilla no encontrada",
            user_id=user_id,
            meta=meta,
        )
        db.add(log)
        db.flush()
        return log

    if not template.enabled:
        log = EmailLog(
            template_slug=slug,
            recipient=recipient,
            subject=template.subject,
            status="skipped",
            error_message="Plantilla deshabilitada",
            user_id=user_id,
            meta=meta,
        )
        db.add(log)
        db.flush()
        return log

    if recipient.endswith("@guest.local"):
        log = EmailLog(
            template_slug=slug,
            recipient=recipient,
            subject=template.subject,
            status="skipped",
            error_message="Correo invitado no enviable",
            user_id=user_id,
            meta=meta,
        )
        db.add(log)
        db.flush()
        return log

    subject = render_template(template.subject, context)
    html = render_template(template.html_body, context)
    text = render_template(template.text_body, context)

    # Si se pasó recipient_name o está en context (user_name, member_name, musician_name)
    target_name = recipient_name or context.get("user_name") or context.get("member_name") or context.get("musician_name")

    try:
        provider_id = send_via_brevo(
            to=recipient,
            subject=subject,
            html=html,
            text=text,
            recipient_name=target_name,
            extra_attachments=extra_attachments,
        )
        status = "sent" if provider_id else "skipped"
        error = None if provider_id else "Envío omitido (EMAIL_ENABLED o BREVO_API_KEY)"
    except EmailDeliveryError as exc:
        status = "failed"
        provider_id = None
        error = str(exc)

    log = EmailLog(
        template_slug=slug,
        recipient=recipient,
        subject=subject,
        status=status,
        error_message=error,
        provider_message_id=provider_id,
        user_id=user_id,
        meta=meta,
    )
    db.add(log)
    db.flush()
    return log
