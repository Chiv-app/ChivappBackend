from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.user import User, UserRole


def notify_user(
    db: Session,
    *,
    user: User,
    type: str,
    title: str,
    message: str,
    booking_id: str | None = None,
    meta: dict | None = None,
) -> None:
    payload: dict = dict(meta or {})
    if booking_id:
        payload["booking_id"] = booking_id

    db.add(
        Notification(
            user_id=user.id,
            type=type,
            title=title,
            message=message,
            meta=payload or None,
        )
    )


def notify_admins(
    db: Session,
    *,
    type: str,
    title: str,
    message: str,
    meta: dict | None = None,
) -> None:
    admins = db.query(User).filter(User.role == UserRole.admin).all()
    for admin in admins:
        notify_user(
            db,
            user=admin,
            type=type,
            title=title,
            message=message,
            meta=meta,
        )


def notify_booking_created(db: Session, musician_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_requested",
        title="Nueva solicitud de reserva",
        message="Un contratista te enviÃ³ una solicitud de reserva. RevÃ­sala y responde con tu cotizaciÃ³n.",
        booking_id=booking_id,
    )

    # EnvÃ­o de correo transaccional enriquecido al mÃºsico
    try:
        from app.core.config import settings
        from app.models.booking import Booking
        from app.models.contractor_profile import ContractorProfile
        from app.services.email.defaults import APP_NAME
        from app.services.email.service import send_templated_email

        booking = db.query(Booking).filter(Booking.id == booking_id).first()
        if booking and musician_user.email and not musician_user.email.endswith("@guest.local"):
            contractor_name = "Cliente"
            if booking.contractor_id:
                contractor = (
                    db.query(ContractorProfile)
                    .filter(ContractorProfile.id == booking.contractor_id)
                    .first()
                )
                if contractor and contractor.user and contractor.user.fullname:
                    contractor_name = contractor.user.fullname

            event_date_str = (
                booking.event_date.strftime("%d/%m/%Y")
                if booking.event_date
                else "Por coordinar"
            )
            event_time_str = (
                booking.start_time.strftime("%H:%M")
                if booking.start_time
                else "Por coordinar"
            )
            location_str = booking.location_address or "Por definir"
            if booking.location_city:
                location_str = f"{location_str}, {booking.location_city}"

            action_url = f"{settings.FRONTEND_URL.rstrip('/')}/musician/bookings/{booking_id}"

            send_templated_email(
                db,
                slug="booking_new_request",
                to=musician_user.email,
                context={
                    "musician_name": musician_user.fullname or "MÃºsico",
                    "contractor_name": contractor_name,
                    "event_type": booking.event_type or "PresentaciÃ³n musical",
                    "event_date": event_date_str,
                    "event_time": event_time_str,
                    "event_location": location_str,
                    "event_description": booking.event_description or "Sin detalles adicionales",
                    "action_url": action_url,
                    "app_name": APP_NAME,
                },
                user_id=musician_user.id,
                meta={"booking_id": booking_id},
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Error enviando correo de nueva solicitud de reserva: %s", exc
        )



def notify_booking_updated(db: Session, musician_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_updated",
        title="Solicitud actualizada",
        message="El contratista actualizÃ³ los detalles de la solicitud. RevÃ­sala antes de cotizar.",
        booking_id=booking_id,
    )


def notify_booking_quoted(db: Session, contractor_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_quoted",
        title="CotizaciÃ³n recibida",
        message="El mÃºsico respondiÃ³ tu solicitud con un precio. Revisa la cotizaciÃ³n y confirma o rechaza.",
        booking_id=booking_id,
    )

    try:
        from app.core.config import settings
        from app.models.booking import Booking
        from app.models.musician_profile import MusicianProfile
        from app.services.email.defaults import APP_NAME
        from app.services.email.service import send_templated_email

        booking = db.query(Booking).filter(Booking.id == booking_id).first()
        if booking and contractor_user.email and not contractor_user.email.endswith("@guest.local"):
            musician_name = "El mÃºsico"
            if booking.musician_id:
                musician = (
                    db.query(MusicianProfile)
                    .filter(MusicianProfile.id == booking.musician_id)
                    .first()
                )
                if musician and (musician.stage_name or (musician.user and musician.user.fullname)):
                    musician_name = musician.stage_name or musician.user.fullname

            price_str = f"{booking.price:.2f}" if booking.price else "0.00"
            action_url = f"{settings.FRONTEND_URL.rstrip('/')}/contractor/bookings/{booking_id}"
            send_templated_email(
                db,
                slug="booking_quoted",
                to=contractor_user.email,
                context={
                    "contractor_name": contractor_user.fullname or "Cliente",
                    "musician_name": musician_name,
                    "event_type": booking.event_type or "PresentaciÃ³n musical",
                    "price": price_str,
                    "app_name": APP_NAME,
                    "action_url": action_url,
                },
                user_id=contractor_user.id,
                meta={"booking_id": booking_id},
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Error enviando correo de cotizaciÃ³n recibida: %s", exc
        )


def notify_booking_quote_updated(db: Session, contractor_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_quote_updated",
        title="CotizaciÃ³n actualizada",
        message="El mÃºsico modificÃ³ su cotizaciÃ³n. Revisa los cambios y confirma o rechaza.",
        booking_id=booking_id,
    )


def notify_booking_quote_accepted(db: Session, musician_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_quote_accepted",
        title="CotizaciÃ³n aceptada",
        message="El contratista aceptÃ³ tu cotizaciÃ³n. El contrato estÃ¡ listo para su revisiÃ³n.",
        booking_id=booking_id,
    )


def notify_booking_confirmed(db: Session, musician_user: User, booking_id: str, contractor_user: User = None) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_confirmed",
        title="¡Reserva confirmada!",
        message="El pago fue aprobado exitosamente y la reserva está confirmada.",
        booking_id=booking_id,
    )
    if contractor_user:
        notify_user(
            db,
            user=contractor_user,
            type="booking_confirmed",
            title="¡Reserva confirmada!",
            message="Tu pago fue aprobado exitosamente y la reserva está confirmada.",
            booking_id=booking_id,
        )

    try:
        from app.models.booking import Booking
        from app.services.email.defaults import APP_NAME
        from app.core.config import settings
        from app.services.email.service import send_templated_email

        booking = db.query(Booking).filter(Booking.id == booking_id).first()
        if not booking:
            return

        # Musician Email
        if musician_user.email and not musician_user.email.endswith("@guest.local"):
            action_url = f"{settings.FRONTEND_URL.rstrip('/')}/musician/bookings/{booking_id}"
            send_templated_email(
                db,
                slug="booking_confirmed_musician",
                to=musician_user.email,
                context={
                    "musician_name": musician_user.fullname or "Músico",
                    "event_type": booking.event_type or "Evento",
                    "app_name": APP_NAME,
                    "action_url": action_url,
                },
                user_id=musician_user.id,
                meta={"booking_id": booking_id},
            )

        # Contractor Email
        if contractor_user and contractor_user.email and not contractor_user.email.endswith("@guest.local"):
            action_url_cont = f"{settings.FRONTEND_URL.rstrip('/')}/contractor/bookings/{booking_id}"
            send_templated_email(
                db,
                slug="booking_confirmed_contractor",
                to=contractor_user.email,
                context={
                    "contractor_name": contractor_user.fullname or "Cliente",
                    "event_type": booking.event_type or "Evento",
                    "app_name": APP_NAME,
                    "action_url": action_url_cont,
                },
                user_id=contractor_user.id,
                meta={"booking_id": booking_id},
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Error enviando correos de confirmacion: %s", exc)

def notify_admins_payment_submitted(
    db: Session, booking_id: str, *, kind: str
) -> None:
    label = "el anticipo" if kind == "advance" else "el abono final"
    notify_admins(
        db,
        type="admin_payment_review_requested",
        title="Comprobante por validar",
        message=f"Un contratista subiÃ³ el comprobante de {label}. RevÃ­salo en TesorerÃ­a.",
        meta={"booking_id": booking_id, "kind": kind},
    )


def notify_payment_validated(
    db: Session, contractor_user: User, musician_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="payment_validated",
        title="Reserva confirmada",
        message="El administrador validÃ³ tu anticipo. Ya puedes coordinar detalles del evento.",
        booking_id=booking_id,
    )
    notify_user(
        db,
        user=musician_user,
        type="payment_validated",
        title="Reserva confirmada",
        message="El administrador validÃ³ el anticipo del contratista. La reserva quedÃ³ confirmada.",
        booking_id=booking_id,
    )


def notify_payment_rejected(
    db: Session,
    contractor_user: User,
    musician_user: User,
    booking_id: str,
    *,
    reason: str,
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="payment_rejected",
        title="Comprobante rechazado",
        message=f"El administrador rechazÃ³ tu comprobante: {reason}. Vuelve a firmar y subir la evidencia.",
        booking_id=booking_id,
    )
    notify_user(
        db,
        user=musician_user,
        type="payment_rejected",
        title="Comprobante rechazado",
        message="El administrador rechazÃ³ el comprobante del anticipo de tu contratista.",
        booking_id=booking_id,
    )


def notify_payment_released(db: Session, musician_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=musician_user,
        type="payment_released",
        title="Pago liberado",
        message="Los pagos de esta reserva fueron liberados. Ya puedes verlos en tus ingresos.",
        booking_id=booking_id,
    )


def notify_booking_commitment_updated(
    db: Session,
    contractor_user: User,
    booking_id: str,
    *,
    notes: str | None = None,
) -> None:
    """MÃºsico actualiza el compromiso: solo notifica al contratista (sin validaciÃ³n)."""
    base = (
        "El mÃºsico actualizÃ³ detalles del compromiso (ubicaciÃ³n, precio u otros). "
        "Revisa la reserva."
    )
    if notes and notes.strip():
        base = f"{base} Nota: {notes.strip()[:120]}"
    notify_user(
        db,
        user=contractor_user,
        type="booking_commitment_updated",
        title="Compromiso actualizado",
        message=base,
        booking_id=booking_id,
    )


def notify_booking_change_requested(
    db: Session, musician_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_change_requested",
        title="Cambio solicitado en la reserva",
        message="El contratista propuso cambios (ubicaciÃ³n u otros detalles). RevÃ­salos y acepta o rechaza.",
        booking_id=booking_id,
    )


def notify_booking_change_accepted(
    db: Session, contractor_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_change_accepted",
        title="Cambio aceptado",
        message="El mÃºsico aceptÃ³ los cambios que propusiste. La reserva se actualizÃ³ sin volver a firmar.",
        booking_id=booking_id,
    )


def notify_booking_change_rejected(
    db: Session, contractor_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_change_rejected",
        title="Cambio rechazado",
        message="El mÃºsico rechazÃ³ los cambios propuestos. La reserva continÃºa con los datos anteriores.",
        booking_id=booking_id,
    )


def notify_booking_requoted(
    db: Session, contractor_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_quoted",
        title="Nueva cotizaciÃ³n por cambios",
        message="El mÃºsico actualizÃ³ el precio tras tus cambios. Revisa la nueva cotizaciÃ³n.",
        booking_id=booking_id,
    )


def notify_booking_message(
    db: Session, target_user: User, booking_id: str, preview: str
) -> None:
    notify_user(
        db,
        user=target_user,
        type="booking_message",
        title="Nuevo mensaje en tu reserva",
        message=preview[:140],
        booking_id=booking_id,
    )


def notify_balance_submitted(
    db: Session, musician_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=musician_user,
        type="balance_submitted",
        title="Abono final en revisiÃ³n",
        message="El contratista subiÃ³ el comprobante del saldo. Un administrador lo revisarÃ¡.",
        booking_id=booking_id,
    )


def notify_balance_validated(
    db: Session, contractor_user: User, musician_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="balance_validated",
        title="Abono final validado",
        message="El administrador validÃ³ el pago total. Ya puedes compartir fotos y reseÃ±a del evento.",
        booking_id=booking_id,
    )
    notify_user(
        db,
        user=musician_user,
        type="balance_validated",
        title="Abono final validado",
        message="El administrador validÃ³ el pago total del contratista. Ya se habilitÃ³ la fase de evento.",
        booking_id=booking_id,
    )


def notify_balance_rejected(
    db: Session,
    contractor_user: User,
    musician_user: User,
    booking_id: str,
    *,
    reason: str,
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="balance_rejected",
        title="Abono final rechazado",
        message=f"El administrador rechazÃ³ el comprobante del saldo: {reason}. Vuelve a subirlo.",
        booking_id=booking_id,
    )
    notify_user(
        db,
        user=musician_user,
        type="balance_rejected",
        title="Abono final rechazado",
        message="El administrador rechazÃ³ el comprobante del saldo de tu contratista.",
        booking_id=booking_id,
    )


def notify_booking_event_started(
    db: Session, target_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=target_user,
        type="booking_event_started",
        title="Evento habilitado",
        message="La fase de evento quedÃ³ activa. Ya pueden compartir fotos y reacciones.",
        booking_id=booking_id,
    )


def notify_live_location_requested(
    db: Session,
    target_user: User,
    booking_id: str,
    from_party: str,
) -> None:
    who = {
        "leader": "El mÃºsico lÃ­der",
        "member": "Un integrante",
        "musician": "El mÃºsico",
        "contractor": "El contratista",
    }.get(from_party, "Un participante")
    notify_user(
        db,
        user=target_user,
        type="live_location_requested",
        title="Solicitud de ubicaciÃ³n",
        message=f"{who} te pide compartir tu ubicaciÃ³n en vivo durante el evento.",
        booking_id=booking_id,
    )


def notify_live_location_sharing(
    db: Session,
    target_user: User,
    booking_id: str,
    from_party: str,
) -> None:
    who = {
        "leader": "El mÃºsico lÃ­der",
        "member": "Un integrante",
        "musician": "El mÃºsico",
        "contractor": "El contratista",
    }.get(from_party, "Un participante")
    notify_user(
        db,
        user=target_user,
        type="live_location_sharing",
        title="UbicaciÃ³n compartida",
        message=f"{who} empezÃ³ a compartir su ubicaciÃ³n. Ya puedes verla en el mapa.",
        booking_id=booking_id,
    )


def notify_booking_completed(
    db: Session, target_user: User, booking_id: str
) -> None:
    notify_user(
        db,
        user=target_user,
        type="booking_completed",
        title="Reserva finalizada",
        message="La contrataciÃ³n quedÃ³ cerrada. Gracias por usar la plataforma.",
        booking_id=booking_id,
    )


def notify_booking_review(
    db: Session, target_user: User, booking_id: str, preview: str
) -> None:
    notify_user(
        db,
        user=target_user,
        type="booking_review",
        title="Nueva reacciÃ³n del evento",
        message=preview[:140],
        booking_id=booking_id,
    )


def notify_booking_rejected(
    db: Session,
    target_user: User,
    booking_id: str,
    *,
    by_role: str,
) -> None:
    role_label = "mÃºsico" if by_role == "musician" else "contratista"
    notify_user(
        db,
        user=target_user,
        type="booking_rejected",
        title="Solicitud rechazada",
        message=f"La solicitud de reserva fue rechazada por el {role_label}.",
        booking_id=booking_id,
    )


def notify_booking_cancelled(
    db: Session,
    target_user: User,
    booking_id: str,
    *,
    by_role: str,
) -> None:
    role_label = "mÃºsico" if by_role == "musician" else "contratista"
    notify_user(
        db,
        user=target_user,
        type="booking_cancelled",
        title="Reserva cancelada",
        message=f"La reserva fue cancelada por el {role_label}.",
        booking_id=booking_id,
    )


def notify_profile_submitted(
    db: Session,
    *,
    user: User,
    profile_role: str,
    profile_id: str,
    display_name: str,
) -> None:
    role_label = "mÃºsico" if profile_role == "musician" else "contratista"
    notify_user(
        db,
        user=user,
        type="profile_submitted",
        title="Perfil enviado a revisiÃ³n",
        message="Recibimos tu solicitud de verificaciÃ³n. Te avisaremos cuando un administrador la revise.",
        meta={"profile_id": profile_id, "profile_role": profile_role},
    )
    notify_admins(
        db,
        type="profile_review_requested",
        title=f"Nueva verificaciÃ³n de {role_label}",
        message=f"{display_name} enviÃ³ su perfil de {role_label} para revisiÃ³n.",
        meta={"profile_id": profile_id, "profile_role": profile_role},
    )


def notify_profile_approved(
    db: Session,
    *,
    user: User,
    profile_role: str,
    profile_id: str,
) -> None:
    role_label = "mÃºsico" if profile_role == "musician" else "contratista"
    notify_user(
        db,
        user=user,
        type="profile_approved",
        title="Perfil verificado",
        message=f"Tu perfil de {role_label} fue aprobado. Ya puedes usar todas las funciones de la plataforma.",
        meta={"profile_id": profile_id, "profile_role": profile_role},
    )

    try:
        from app.core.config import settings
        from app.services.email.defaults import APP_NAME
        from app.services.email.service import send_templated_email

        if user.email and not user.email.endswith("@guest.local"):
            dashboard_path = (
                "/musician/profile" if profile_role == "musician" else "/contractor/profile"
            )
            action_url = f"{settings.FRONTEND_URL.rstrip('/')}{dashboard_path}"
            send_templated_email(
                db,
                slug="profile_approved",
                to=user.email,
                context={
                    "user_name": user.fullname or "Usuario",
                    "role_label": role_label.capitalize(),
                    "app_name": APP_NAME,
                    "action_url": action_url,
                },
                user_id=user.id,
                meta={"profile_id": profile_id, "profile_role": profile_role},
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Error enviando correo de perfil aprobado: %s", exc
        )


def notify_profile_rejected(
    db: Session,
    *,
    user: User,
    profile_role: str,
    profile_id: str,
    reason: str,
) -> None:
    role_label = "mÃºsico" if profile_role == "musician" else "contratista"
    notify_user(
        db,
        user=user,
        type="profile_rejected",
        title="Perfil rechazado",
        message=f"Tu perfil necesita correcciones: {reason}",
        meta={
            "profile_id": profile_id,
            "profile_role": profile_role,
            "rejection_reason": reason,
        },
    )

    try:
        from app.core.config import settings
        from app.services.email.defaults import APP_NAME
        from app.services.email.service import send_templated_email

        if user.email and not user.email.endswith("@guest.local"):
            dashboard_path = (
                "/musician/profile" if profile_role == "musician" else "/contractor/profile"
            )
            action_url = f"{settings.FRONTEND_URL.rstrip('/')}{dashboard_path}"
            send_templated_email(
                db,
                slug="profile_rejected",
                to=user.email,
                context={
                    "user_name": user.fullname or "Usuario",
                    "role_label": role_label.capitalize(),
                    "reason": reason,
                    "app_name": APP_NAME,
                    "action_url": action_url,
                },
                user_id=user.id,
                meta={
                    "profile_id": profile_id,
                    "profile_role": profile_role,
                    "rejection_reason": reason,
                },
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Error enviando correo de perfil rechazado: %s", exc
        )


def notify_profile_needs_resubmit(
    db: Session,
    *,
    user: User,
    profile_role: str,
    profile_id: str,
) -> None:
    notify_user(
        db,
        user=user,
        type="profile_needs_resubmit",
        title="VerificaciÃ³n pendiente otra vez",
        message="Actualizaste tu perfil publicado. Vuelve a enviarlo para revisiÃ³n para recuperar la verificaciÃ³n.",
        meta={"profile_id": profile_id, "profile_role": profile_role},
    )


def notify_complaint_opened(db: Session, musician_user: User, booking_id: str) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_complaint_opened",
        title="Queja sobre un show finalizado",
        message="El contratista registrÃ³ una queja. Revisa el detalle y acepta o presenta tu descargo.",
        booking_id=booking_id,
    )
    notify_admins(
        db,
        type="booking_complaint_opened",
        title="Nueva queja de reserva",
        message="Un contratista abriÃ³ una queja. Espera las respuestas antes de liquidar.",
        meta={"booking_id": booking_id},
    )


def notify_complaint_musician_update(
    db: Session,
    *,
    contractor_user: User,
    booking_id: str,
    accepted: bool,
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_complaint_musician_update",
        title="El mÃºsico respondiÃ³ la queja" if not accepted else "El mÃºsico aceptÃ³ la queja",
        message=(
            "El mÃºsico aceptÃ³ la queja. El admin definirÃ¡ los montos al liquidar."
            if accepted
            else "El mÃºsico presentÃ³ su descargo. El admin revisarÃ¡ y liquidarÃ¡."
        ),
        booking_id=booking_id,
    )
    notify_admins(
        db,
        type="booking_complaint_musician_update",
        title="Queja lista para liquidar",
        message="Ambas partes ya respondieron. Define montos y liquida desde TesorerÃ­a.",
        meta={"booking_id": booking_id, "accepted": accepted},
    )


def notify_settlement_completed(
    db: Session,
    *,
    musician_user: User,
    contractor_user: User,
    booking_id: str,
    musician_amount: float,
    contractor_refund: float,
) -> None:
    notify_user(
        db,
        user=musician_user,
        type="booking_settled",
        title="Desembolso autorizado",
        message=f"El admin liquidÃ³ la reserva. Monto a recibir: S/ {musician_amount:.2f}.",
        booking_id=booking_id,
    )
    if contractor_refund > 0:
        notify_user(
            db,
            user=contractor_user,
            type="booking_settled_refund",
            title="DevoluciÃ³n pendiente de transferencia",
            message=(
                f"El admin liquidÃ³ la disputa con una devoluciÃ³n de "
                f"S/ {contractor_refund:.2f}. Cuando envÃ­en el comprobante "
                f"deberÃ¡s validarlo."
            ),
            booking_id=booking_id,
        )


def notify_refund_transfer_sent(
    db: Session,
    *,
    contractor_user: User,
    booking_id: str,
    amount: float,
) -> None:
    notify_user(
        db,
        user=contractor_user,
        type="booking_refund_transfer",
        title="Comprobante de devoluciÃ³n",
        message=(
            f"El admin registrÃ³ la devoluciÃ³n de S/ {amount:.2f}. "
            f"Revisa el comprobante y valida para completar."
        ),
        booking_id=booking_id,
    )


def notify_refund_validated(
    db: Session,
    *,
    booking_id: str,
    amount: float,
    accepted: bool,
    reason: str | None = None,
) -> None:
    if accepted:
        notify_admins(
            db,
            type="booking_refund_validated",
            title="DevoluciÃ³n confirmada",
            message=(
                f"El contratista validÃ³ la devoluciÃ³n de S/ {amount:.2f}."
            ),
            meta={"booking_id": booking_id, "amount": amount},
        )
    else:
        detail = f" Motivo: {reason}" if reason else ""
        notify_admins(
            db,
            type="booking_refund_rejected",
            title="DevoluciÃ³n rechazada",
            message=(
                f"El contratista rechazÃ³ el comprobante de S/ {amount:.2f}."
                f"{detail} Debes volver a transferir."
            ),
            meta={"booking_id": booking_id, "amount": amount, "reason": reason},
        )


