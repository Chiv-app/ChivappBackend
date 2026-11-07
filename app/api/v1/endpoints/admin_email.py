from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api import deps
from app.models.email_log import EmailLog
from app.models.email_template import EmailTemplate
from app.models.user import User
from app.services.admin_audit import record_admin_audit
from app.schemas.email import (
    EmailLogOut,
    EmailTemplateOut,
    EmailTemplatePreviewRequest,
    EmailTemplateRenderOut,
    EmailTemplateRenderRequest,
    EmailTemplateUpdate,
)
from app.services.email.logo import LOGO_CID, LOGO_PUBLIC_URL
from app.services.email.preview_context import build_sample_email_context
from app.services.email.service import get_template, render_email_template, send_templated_email

router = APIRouter(prefix="/admin/email", tags=["Admin Email"])


@router.get("/templates", response_model=list[EmailTemplateOut])
def list_email_templates(
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_admin),
):
    return (
        db.query(EmailTemplate)
        .order_by(EmailTemplate.name.asc())
        .all()
    )


@router.get("/templates/{slug}", response_model=EmailTemplateOut)
def get_email_template(
    slug: str,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_admin),
):
    template = get_template(db, slug)
    if not template:
        raise HTTPException(404, "Plantilla no encontrada")
    return template


@router.patch("/templates/{slug}", response_model=EmailTemplateOut)
def update_email_template(
    slug: str,
    payload: EmailTemplateUpdate,
    db: Session = Depends(deps.get_db),
    admin: User = Depends(deps.get_current_admin),
):
    template = get_template(db, slug)
    if not template:
        raise HTTPException(404, "Plantilla no encontrada")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(template, field, value)
    template.updated_at = datetime.utcnow()
    # Se registra qué campos cambiaron, no el contenido completo de la plantilla.
    record_admin_audit(
        db,
        admin_user=admin,
        action="email_template.update",
        target_type="email_template",
        target_id=slug,
        after_state={"fields_changed": sorted(data.keys())},
    )
    db.commit()
    db.refresh(template)
    return template


@router.post("/templates/{slug}/preview", response_model=EmailLogOut)
def preview_email_template(
    slug: str,
    payload: EmailTemplatePreviewRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_admin),
):
    template = get_template(db, slug)
    if not template:
        raise HTTPException(404, "Plantilla no encontrada")

    sample_context = build_sample_email_context(current_user)
    sample_context.update(payload.context)

    recipient = str(payload.to or current_user.email)
    record_admin_audit(
        db,
        admin_user=current_user,
        action="email_template.preview_send",
        target_type="email_template",
        target_id=slug,
        after_state={"recipient": recipient},
    )
    log = send_templated_email(
        db,
        slug=slug,
        to=recipient,
        context=sample_context,
        user_id=current_user.id,
        meta={"preview": True},
    )
    db.commit()
    db.refresh(log)
    return log


@router.post("/templates/{slug}/render", response_model=EmailTemplateRenderOut)
def render_email_template_preview(
    slug: str,
    payload: EmailTemplateRenderRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_admin),
):
    template = get_template(db, slug)
    if not template:
        raise HTTPException(404, "Plantilla no encontrada")

    sample_context = build_sample_email_context(current_user)
    sample_context.update(payload.context)

    rendered = render_email_template(
        template,
        context=sample_context,
        subject=payload.subject,
        html_body=payload.html_body,
        text_body=payload.text_body,
    )
    html = rendered["html"].replace(f"cid:{LOGO_CID}", LOGO_PUBLIC_URL)
    return EmailTemplateRenderOut(
        subject=rendered["subject"],
        html=html,
        text=rendered["text"],
        context=sample_context,
    )


@router.get("/logs", response_model=list[EmailLogOut])
def list_email_logs(
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_admin),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = None,
    template_slug: str | None = None,
    q: str | None = None,
):
    query = db.query(EmailLog)
    if status:
        query = query.filter(EmailLog.status == status)
    if template_slug:
        query = query.filter(EmailLog.template_slug == template_slug)
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(
            (EmailLog.recipient.ilike(term)) | (EmailLog.subject.ilike(term))
        )
    return (
        query.order_by(EmailLog.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
