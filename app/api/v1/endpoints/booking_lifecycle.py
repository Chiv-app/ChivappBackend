from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import deps
from app.api.booking_helpers import (
    assert_booking_collaborator,
    assert_booking_contractor_owner,
    assert_booking_musician_owner,
    assert_booking_participant,
    assert_booking_viewer,
    get_booking_or_404,
)
from app.models.booking import Booking, BookingMessage, BookingReview, BookingStatus
from app.models.musician_profile import MusicianProfile
from app.models.payment import Payment, PaymentStatus
from app.models.user import User, UserRole
from app.models.contractor_recommendation import ContractorRecommendation
from app.schemas.booking import (
    BookingBalancePayment,
    BookingChangeDecision,
    BookingEventChangeRequest,
    BookingFinalReviewCreate,
    BookingMessageCreate,
    BookingMessageOut,
    BookingOut,
    BookingReviewCreate,
    BookingReviewOut,
    ContractorRecommendationCreate,
    ContractorRecommendationOut,
)
from app.schemas.settlement import BookingComplaintRespond, BookingRefundReject
from app.services.booking_lifecycle import (
    apply_commitment_fields,
    apply_pending_changes,
    clear_pending_changes,
    is_pre_event,
    remaining_balance,
    retained_paid_total,
)
from app.services.booking_notifications import (
    notify_admins_payment_submitted,
    notify_balance_submitted,
    notify_booking_change_accepted,
    notify_booking_change_rejected,
    notify_booking_change_requested,
    notify_booking_commitment_updated,
    notify_booking_completed,
    notify_booking_event_started,
    notify_booking_message,
    notify_booking_review,
    notify_complaint_musician_update,
    notify_complaint_opened,
)
from app.services.booking_share import (
    disable_booking_share,
    serialize_review,
)
from app.services.ratings import (
    recompute_contractor_rating,
    recompute_musician_rating,
)
from app.services.payment_evidence import (
    apply_evidence_urls,
    assert_evidence_uploads,
    normalize_evidence_urls,
)

router = APIRouter(prefix="/bookings", tags=["Booking lifecycle"])


def _musician_user(db: Session, booking: Booking) -> User:
    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    if not musician:
        raise HTTPException(404, "Músico no encontrado")
    user = db.query(User).filter(User.id == musician.user_id).first()
    if not user:
        raise HTTPException(404, "Usuario del músico no encontrado")
    return user


def _contractor_user_from_booking(db: Session, booking: Booking) -> User:
    from app.models.contractor_profile import ContractorProfile

    contractor = (
        db.query(ContractorProfile)
        .filter(ContractorProfile.id == booking.contractor_id)
        .first()
    )
    if not contractor:
        raise HTTPException(404, "Contratista no encontrado")
    user = db.query(User).filter(User.id == contractor.user_id).first()
    if not user:
        raise HTTPException(404, "Usuario contratista no encontrado")
    return user


def _message_out(message: BookingMessage, sender_name: str | None = None) -> BookingMessageOut:
    return BookingMessageOut(
        id=message.id,
        booking_id=message.booking_id,
        sender_user_id=message.sender_user_id,
        sender_name=sender_name,
        body=message.body,
        created_at=message.created_at,
    )


@router.get("/{booking_id}/messages", response_model=list[BookingMessageOut])
def list_booking_messages(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)

    rows = (
        db.query(BookingMessage)
        .filter(BookingMessage.booking_id == booking.id)
        .order_by(BookingMessage.created_at.asc())
        .all()
    )
    result: list[BookingMessageOut] = []
    for row in rows:
        sender = db.query(User).filter(User.id == row.sender_user_id).first()
        result.append(_message_out(row, sender.fullname if sender else None))
    return result


@router.post("/{booking_id}/messages", response_model=BookingMessageOut, status_code=201)
def post_booking_message(
    booking_id: str,
    payload: BookingMessageCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_collaborator(db, booking, current_user)

    if booking.status in {BookingStatus.completed, BookingStatus.cancelled}:
        raise HTTPException(
            400,
            "La conversación no está disponible en reservas finalizadas o canceladas",
        )

    if booking.status not in {
        BookingStatus.payment_retained,
        BookingStatus.change_pending,
        BookingStatus.balance_pending,
        BookingStatus.balance_review,
        BookingStatus.in_progress,
        BookingStatus.payment_released,
    }:
        raise HTTPException(400, "El chat solo está disponible en reservas confirmadas")

    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "El mensaje no puede estar vacío")

    message = BookingMessage(
        booking_id=booking.id,
        sender_user_id=current_user.id,
        body=body,
    )
    db.add(message)

    if current_user.role == UserRole.contractor:
        target = _musician_user(db, booking)
    else:
        target = _contractor_user_from_booking(db, booking)
    notify_booking_message(db, target, str(booking.id), body)

    db.commit()
    db.refresh(message)
    return _message_out(message, current_user.fullname)


@router.post("/{booking_id}/request-change", response_model=BookingOut)
def request_booking_change(
    booking_id: str,
    payload: BookingEventChangeRequest,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """
    Músico: aplica cambios de inmediato y solo notifica al contratista.
    Contratista: deja cambios pendientes de validación del músico.
    """
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    if current_user.role not in {UserRole.contractor, UserRole.musician}:
        raise HTTPException(403, "No autorizado")

    if booking.status != BookingStatus.payment_retained:
        raise HTTPException(
            400,
            "Solo puedes pedir cambios cuando la reserva está confirmada "
            "(antes del abono final o de iniciar el evento)",
        )
    if not is_pre_event(booking):
        raise HTTPException(400, "Ya no se pueden editar detalles: el evento ya comenzó")

    # --- Músico: aplica al instante (sin validación) ---
    if current_user.role == UserRole.musician:
        has_change = any(
            [
                payload.location_address is not None,
                payload.location_city is not None,
                payload.location_reference is not None,
                payload.event_description is not None,
                payload.price_agreed is not None,
                payload.advance_amount is not None,
            ]
        )
        if not has_change:
            raise HTTPException(400, "Indica al menos un cambio")

        paid = retained_paid_total(db, booking.id)
        next_price = (
            payload.price_agreed
            if payload.price_agreed is not None
            else booking.price_agreed
        )
        next_advance = (
            payload.advance_amount
            if payload.advance_amount is not None
            else booking.advance_amount
        )

        if next_price is not None:
            from app.services.platform_payment import (
                compute_platform_fee_amount,
                get_or_create_platform_payment_settings,
            )

            settings = get_or_create_platform_payment_settings(db)
            next_percent = Decimal(str(settings.platform_fee_percent or 0))
            if booking.platform_fee_percent is not None and payload.price_agreed is None:
                next_percent = Decimal(str(booking.platform_fee_percent))
            next_fee = compute_platform_fee_amount(Decimal(str(next_price)), next_percent)
            if paid > Decimal(str(next_price)) + next_fee:
                raise HTTPException(
                    400,
                    "El total a pagar (precio + comisión) no puede ser menor a lo ya validado",
                )
        if (
            next_price is not None
            and next_advance is not None
            and Decimal(str(next_advance)) > Decimal(str(next_price))
        ):
            raise HTTPException(400, "El anticipo no puede ser mayor al precio")

        # El monto validado (pagos retained/released) no se modifica aquí.
        apply_commitment_fields(
            booking,
            db,
            location_address=payload.location_address,
            location_city=payload.location_city,
            location_reference=payload.location_reference,
            event_description=payload.event_description,
            price_agreed=payload.price_agreed,
            advance_amount=payload.advance_amount,
        )

        notify_booking_commitment_updated(
            db,
            _contractor_user_from_booking(db, booking),
            str(booking.id),
            notes=payload.change_notes,
        )
        db.commit()
        db.refresh(booking)
        return booking

    # --- Contratista: requiere validación del músico ---
    if payload.price_agreed is not None or payload.advance_amount is not None:
        raise HTTPException(
            400,
            "El contratista no puede modificar el precio ni el anticipo",
        )

    has_change = any(
        [
            payload.location_address is not None,
            payload.location_city is not None,
            payload.location_reference is not None,
            payload.event_description is not None,
            bool(payload.change_notes and payload.change_notes.strip()),
        ]
    )
    if not has_change:
        raise HTTPException(400, "Indica al menos un cambio")

    booking.pending_location_address = (
        payload.location_address
        if payload.location_address is not None
        else booking.location_address
    )
    booking.pending_location_city = (
        payload.location_city
        if payload.location_city is not None
        else booking.location_city
    )
    booking.pending_location_reference = (
        payload.location_reference
        if payload.location_reference is not None
        else booking.location_reference
    )
    booking.pending_event_description = (
        payload.event_description
        if payload.event_description is not None
        else booking.event_description
    )
    booking.pending_price_agreed = booking.price_agreed
    booking.pending_advance_amount = booking.advance_amount
    booking.pending_change_notes = payload.change_notes
    booking.change_requested_by = UserRole.contractor.value
    booking.change_requested_at = datetime.utcnow()
    booking.status = BookingStatus.change_pending

    notify_booking_change_requested(
        db, _musician_user(db, booking), str(booking.id)
    )
    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/decide-change", response_model=BookingOut)
def decide_booking_change(
    booking_id: str,
    payload: BookingChangeDecision,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    if booking.status != BookingStatus.change_pending:
        raise HTTPException(400, "No hay un cambio pendiente para esta reserva")

    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo el músico puede validar estos cambios")

    requester = booking.change_requested_by
    if requester != UserRole.contractor.value:
        raise HTTPException(400, "No hay una solicitud de cambio del contratista")

    notify_target = _contractor_user_from_booking(db, booking)

    # Solo aceptar/rechazar: no reabre cotización ni requiere re-firma.
    if payload.accept:
        next_price = (
            payload.price_agreed
            if payload.price_agreed is not None
            else booking.pending_price_agreed
            if booking.pending_price_agreed is not None
            else booking.price_agreed
        )
        next_advance = (
            payload.advance_amount
            if payload.advance_amount is not None
            else booking.pending_advance_amount
            if booking.pending_advance_amount is not None
            else booking.advance_amount
        )

        paid = retained_paid_total(db, booking.id)
        if next_price is not None:
            from app.services.platform_payment import (
                compute_platform_fee_amount,
            )

            next_percent = Decimal(str(booking.platform_fee_percent or 0))
            next_fee = compute_platform_fee_amount(Decimal(str(next_price)), next_percent)
            if paid > Decimal(str(next_price)) + next_fee:
                raise HTTPException(
                    400,
                    "El total a pagar (precio + comisión) no puede ser menor a lo ya validado",
                )
        if (
            next_price is not None
            and next_advance is not None
            and Decimal(str(next_advance)) > Decimal(str(next_price))
        ):
            raise HTTPException(400, "El anticipo no puede ser mayor al precio")

        if payload.price_agreed is not None:
            booking.pending_price_agreed = payload.price_agreed
        if payload.advance_amount is not None:
            booking.pending_advance_amount = payload.advance_amount

        apply_pending_changes(booking)
        booking.status = BookingStatus.payment_retained
        notify_booking_change_accepted(db, notify_target, str(booking.id))
    else:
        clear_pending_changes(booking)
        booking.status = BookingStatus.payment_retained
        notify_booking_change_rejected(db, notify_target, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/start-event", response_model=BookingOut)
def start_event_phase(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Habilita fotos/reseña cuando no hay saldo pendiente (pago total previo)."""
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    if booking.status != BookingStatus.payment_retained:
        raise HTTPException(400, "La reserva debe estar confirmada")

    due = remaining_balance(db, booking)
    if due > 0:
        raise HTTPException(
            400,
            f"Aún hay un saldo pendiente de S/ {due:.2f}. El contratista debe abonarlo primero.",
        )

    booking.status = BookingStatus.in_progress
    other = (
        _musician_user(db, booking)
        if current_user.role == UserRole.contractor
        else _contractor_user_from_booking(db, booking)
    )
    notify_booking_event_started(db, other, str(booking.id))
    db.commit()
    db.refresh(booking)
    return booking


@router.get("/{booking_id}/reviews", response_model=list[BookingReviewOut])
def list_booking_reviews(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_viewer(db, booking, current_user)
    reviews = (
        db.query(BookingReview)
        .filter(BookingReview.booking_id == booking.id)
        .order_by(BookingReview.created_at.asc())
        .all()
    )
    return [serialize_review(review) for review in reviews]


@router.get("/{booking_id}/review", response_model=BookingReviewOut | None)
def get_booking_review(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    """Legacy: returns the latest review if any."""
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)
    review = (
        db.query(BookingReview)
        .filter(BookingReview.booking_id == booking.id)
        .order_by(BookingReview.created_at.desc())
        .first()
    )
    return serialize_review(review) if review else None


@router.post("/{booking_id}/review", response_model=BookingReviewOut)
def create_booking_review(
    booking_id: str,
    payload: BookingReviewCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    if booking.status == BookingStatus.completed:
        raise HTTPException(
            400,
            "Esta reserva finalizada es solo de lectura",
        )

    if booking.status not in {
        BookingStatus.in_progress,
        BookingStatus.payment_released,
    }:
        raise HTTPException(
            400,
            "Las reseñas se habilitan después de validar el abono total / inicio del evento",
        )

    is_final = bool(payload.is_final)
    comment = (payload.comment or "").strip()
    if is_final:
        if len(comment) < 10:
            raise HTTPException(
                400,
                "La reseña final requiere un comentario de al menos 10 caracteres.",
            )
        existing_final = (
            db.query(BookingReview)
            .filter(
                BookingReview.booking_id == booking.id,
                BookingReview.is_final.is_(True),
            )
            .first()
        )
        if existing_final:
            raise HTTPException(400, "Ya existe una reseña final para esta reserva")

    review = BookingReview(
        booking_id=booking.id,
        author_user_id=current_user.id,
        guest_name=None,
        rating=payload.rating,
        emoji=payload.emoji if not is_final else None,
        comment=comment or None,
        photo_urls=payload.photo_urls or [],
        video_urls=payload.video_urls or [],
        is_final=is_final,
    )
    db.add(review)
    db.flush()

    if is_final:
        recompute_musician_rating(db, booking.musician_id)

    musician_user = _musician_user(db, booking)
    preview = comment or payload.emoji or f"{payload.rating}★"
    notify_booking_review(db, musician_user, str(booking.id), preview)

    db.commit()
    db.refresh(review)
    review.author = current_user
    return serialize_review(review)


@router.post("/{booking_id}/final-review")
def create_final_booking_review(
    booking_id: str,
    payload: BookingFinalReviewCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
    from app.services.settlement import serialize_complaint

    reason = (payload.complaint_reason or "").strip()

    # Complaint-only path: no public review; create complaint and finalize in one step.
    if reason:
        booking = get_booking_or_404(db, booking_id)
        assert_booking_contractor_owner(db, booking, current_user)

        if booking.status == BookingStatus.completed:
            existing = (
                db.query(BookingComplaint)
                .filter(BookingComplaint.booking_id == booking.id)
                .first()
            )
            if existing:
                out = serialize_complaint(existing)
                return {
                    "mode": "complaint",
                    "completed": True,
                    "complaint": out.model_dump(mode="json") if out else None,
                }
            raise HTTPException(400, "Esta reserva finalizada es solo de lectura")

        if booking.status not in {
            BookingStatus.in_progress,
            BookingStatus.payment_released,
        }:
            raise HTTPException(
                400,
                "Las quejas se habilitan después de validar el abono total / inicio del evento",
            )

        due = remaining_balance(db, booking)
        if due > 0:
            raise HTTPException(
                400,
                f"Falta el paso «Abono final»: aún hay un saldo pendiente de S/ {due:.2f}.",
            )

        complaint = (
            db.query(BookingComplaint)
            .filter(BookingComplaint.booking_id == booking.id)
            .first()
        )
        if not complaint:
            complaint = BookingComplaint(
                booking_id=booking.id,
                opened_by_user_id=current_user.id,
                reason=reason,
                evidence_url=payload.complaint_evidence_url,
                status=BookingComplaintStatus.open,
            )
            db.add(complaint)
            db.flush()
            musician_user = _musician_user(db, booking)
            notify_complaint_opened(db, musician_user, str(booking.id))
        elif complaint.status == BookingComplaintStatus.settled:
            raise HTTPException(400, "La queja de esta reserva ya fue liquidada")

        booking.status = BookingStatus.completed
        disable_booking_share(booking)

        musician_user = _musician_user(db, booking)
        contractor_user = _contractor_user_from_booking(db, booking)
        other = (
            musician_user
            if current_user.role == UserRole.contractor
            else contractor_user
        )
        notify_booking_completed(db, other, str(booking.id))

        db.commit()
        db.refresh(complaint)
        out = serialize_complaint(complaint)
        return {
            "mode": "complaint",
            "completed": True,
            "complaint": out.model_dump(mode="json") if out else None,
        }

    # Review path — idempotent if a previous attempt already saved the final review.
    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)
    existing_final = (
        db.query(BookingReview)
        .filter(
            BookingReview.booking_id == booking.id,
            BookingReview.is_final.is_(True),
        )
        .first()
    )
    if existing_final:
        review_out = serialize_review(existing_final)
        return {
            "mode": "review",
            "completed": False,
            "review": review_out.model_dump(mode="json"),
        }

    review_out = create_booking_review(
        booking_id,
        BookingReviewCreate(
            rating=payload.rating or 5,
            comment=payload.comment or "",
            is_final=True,
        ),
        current_user,
        db,
    )
    return {
        "mode": "review",
        "completed": False,
        "review": (
            review_out.model_dump(mode="json")
            if hasattr(review_out, "model_dump")
            else review_out
        ),
    }


@router.get("/{booking_id}/complaint")
def get_booking_complaint(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from app.services.settlement import serialize_complaint

    booking = get_booking_or_404(db, booking_id)
    assert_booking_viewer(db, booking, current_user)
    complaint = getattr(booking, "complaint", None)
    if complaint is None:
        from app.models.booking_complaint import BookingComplaint

        complaint = (
            db.query(BookingComplaint)
            .filter(BookingComplaint.booking_id == booking.id)
            .first()
        )
    return serialize_complaint(complaint)


@router.post("/{booking_id}/complaint/accept")
def accept_booking_complaint(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from datetime import datetime

    from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
    from app.services.settlement import serialize_complaint

    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo el músico puede aceptar la queja")

    booking = get_booking_or_404(db, booking_id)
    assert_booking_musician_owner(db, booking, current_user)

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not complaint:
        raise HTTPException(404, "No hay queja en esta reserva")
    if complaint.status != BookingComplaintStatus.open:
        raise HTTPException(400, "La queja ya fue respondida o liquidada")

    complaint.status = BookingComplaintStatus.musician_accepted
    complaint.musician_responded_at = datetime.utcnow()
    complaint.musician_response = "Queja aceptada por el músico."

    contractor_user = _contractor_user_from_booking(db, booking)
    notify_complaint_musician_update(
        db,
        contractor_user=contractor_user,
        booking_id=str(booking.id),
        accepted=True,
    )
    db.commit()
    db.refresh(complaint)
    return serialize_complaint(complaint)


@router.post("/{booking_id}/complaint/respond")
def respond_booking_complaint(
    booking_id: str,
    payload: BookingComplaintRespond,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from datetime import datetime

    from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
    from app.services.settlement import serialize_complaint

    if current_user.role != UserRole.musician:
        raise HTTPException(403, "Solo el músico puede presentar descargo")

    booking = get_booking_or_404(db, booking_id)
    assert_booking_musician_owner(db, booking, current_user)

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not complaint:
        raise HTTPException(404, "No hay queja en esta reserva")
    if complaint.status != BookingComplaintStatus.open:
        raise HTTPException(400, "La queja ya fue respondida o liquidada")

    complaint.status = BookingComplaintStatus.musician_responded
    complaint.musician_response = payload.response.strip()
    complaint.musician_response_evidence_url = payload.evidence_url
    complaint.musician_responded_at = datetime.utcnow()

    contractor_user = _contractor_user_from_booking(db, booking)
    notify_complaint_musician_update(
        db,
        contractor_user=contractor_user,
        booking_id=str(booking.id),
        accepted=False,
    )
    db.commit()
    db.refresh(complaint)
    return serialize_complaint(complaint)


@router.post("/{booking_id}/complaint/validate-refund")
def validate_booking_refund(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from datetime import datetime

    from app.models.booking_complaint import (
        REFUND_STATUS_AWAITING_VALIDATION,
        REFUND_STATUS_COMPLETED,
        BookingComplaint,
        BookingComplaintStatus,
    )
    from app.services.booking_notifications import notify_refund_validated
    from app.services.settlement import complaint_refund_amount, serialize_complaint

    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not complaint or complaint.status != BookingComplaintStatus.settled:
        raise HTTPException(400, "No hay una liquidación con devolución pendiente")
    if complaint.refund_status != REFUND_STATUS_AWAITING_VALIDATION:
        raise HTTPException(400, "No hay un comprobante de devolución por validar")

    amount = complaint_refund_amount(complaint)
    if complaint.refund_payment_id:
        payment = db.get(Payment, complaint.refund_payment_id)
        if payment:
            payment.status = PaymentStatus.refunded
            payment.released_at = datetime.utcnow()

    complaint.refund_status = REFUND_STATUS_COMPLETED
    complaint.refund_validated_at = datetime.utcnow()
    complaint.refund_rejection_reason = None

    notify_refund_validated(
        db,
        booking_id=str(booking.id),
        amount=amount,
        accepted=True,
    )
    db.commit()
    db.refresh(complaint)
    return serialize_complaint(complaint)


@router.post("/{booking_id}/complaint/reject-refund")
def reject_booking_refund(
    booking_id: str,
    payload: BookingRefundReject,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    from app.models.booking_complaint import (
        REFUND_STATUS_AWAITING_VALIDATION,
        REFUND_STATUS_REJECTED,
        BookingComplaint,
        BookingComplaintStatus,
    )
    from app.services.booking_notifications import notify_refund_validated
    from app.services.settlement import complaint_refund_amount, serialize_complaint

    booking = get_booking_or_404(db, booking_id)
    assert_booking_contractor_owner(db, booking, current_user)

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not complaint or complaint.status != BookingComplaintStatus.settled:
        raise HTTPException(400, "No hay una liquidación con devolución pendiente")
    if complaint.refund_status != REFUND_STATUS_AWAITING_VALIDATION:
        raise HTTPException(400, "No hay un comprobante de devolución por rechazar")

    reason = payload.reason.strip()
    amount = complaint_refund_amount(complaint)
    if complaint.refund_payment_id:
        payment = db.get(Payment, complaint.refund_payment_id)
        if payment:
            payment.status = PaymentStatus.failed

    complaint.refund_status = REFUND_STATUS_REJECTED
    complaint.refund_rejection_reason = reason

    notify_refund_validated(
        db,
        booking_id=str(booking.id),
        amount=amount,
        accepted=False,
        reason=reason,
    )
    db.commit()
    db.refresh(complaint)
    return serialize_complaint(complaint)


@router.get(
    "/{booking_id}/recommend-contractor",
    response_model=ContractorRecommendationOut | None,
)
def get_contractor_recommendation(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    recommendation = (
        db.query(ContractorRecommendation)
        .filter(ContractorRecommendation.booking_id == booking.id)
        .first()
    )
    if not recommendation:
        return None

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    return ContractorRecommendationOut(
        id=recommendation.id,
        booking_id=recommendation.booking_id,
        musician_id=recommendation.musician_id,
        contractor_id=recommendation.contractor_id,
        rating=recommendation.rating,
        comment=recommendation.comment,
        musician_name=musician.stage_name if musician else None,
        created_at=recommendation.created_at,
    )


@router.post(
    "/{booking_id}/recommend-contractor",
    response_model=ContractorRecommendationOut,
)
def recommend_contractor(
    booking_id: str,
    payload: ContractorRecommendationCreate,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_musician_owner(db, booking, current_user)

    if booking.status not in {
        BookingStatus.in_progress,
        BookingStatus.payment_released,
        BookingStatus.completed,
    }:
        raise HTTPException(
            400,
            "Solo puedes recomendar al cliente después de la fase de evento.",
        )

    existing = (
        db.query(ContractorRecommendation)
        .filter(ContractorRecommendation.booking_id == booking.id)
        .first()
    )
    if existing:
        raise HTTPException(400, "Ya recomendaste a este cliente para esta reserva")

    comment = payload.comment.strip()
    if len(comment) < 10:
        raise HTTPException(400, "El comentario debe tener al menos 10 caracteres")

    musician = (
        db.query(MusicianProfile)
        .filter(MusicianProfile.id == booking.musician_id)
        .first()
    )
    recommendation = ContractorRecommendation(
        booking_id=booking.id,
        musician_id=booking.musician_id,
        contractor_id=booking.contractor_id,
        rating=payload.rating,
        comment=comment,
    )
    db.add(recommendation)
    db.flush()
    recompute_contractor_rating(db, booking.contractor_id)
    db.commit()
    db.refresh(recommendation)

    return ContractorRecommendationOut(
        id=recommendation.id,
        booking_id=recommendation.booking_id,
        musician_id=recommendation.musician_id,
        contractor_id=recommendation.contractor_id,
        rating=recommendation.rating,
        comment=recommendation.comment,
        musician_name=musician.stage_name if musician else None,
        created_at=recommendation.created_at,
    )


@router.post("/{booking_id}/complete", response_model=BookingOut)
def complete_booking(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_participant(db, booking, current_user)

    if booking.status == BookingStatus.completed:
        return booking

    if booking.status not in {
        BookingStatus.in_progress,
        BookingStatus.payment_released,
    }:
        raise HTTPException(
            400,
            "Aún no puedes finalizar: falta completar la fase de evento. "
            "Revisa el timeline para ver el paso pendiente.",
        )

    final_review = (
        db.query(BookingReview)
        .filter(
            BookingReview.booking_id == booking.id,
            BookingReview.is_final.is_(True),
        )
        .first()
    )
    from app.models.booking_complaint import BookingComplaint

    complaint = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id == booking.id)
        .first()
    )
    if not final_review and not complaint:
        raise HTTPException(
            400,
            "Falta el paso «Evento y reseña»: el contratista debe dejar "
            "una reseña final o registrar una queja antes de finalizar.",
        )

    due = remaining_balance(db, booking)
    if due > 0:
        raise HTTPException(
            400,
            f"Falta el paso «Abono final»: aún hay un saldo pendiente de S/ {due:.2f}.",
        )

    # Funds stay retained in the platform as app debt until an admin settles.
    booking.status = BookingStatus.completed
    disable_booking_share(booking)

    musician_user = _musician_user(db, booking)
    contractor_user = _contractor_user_from_booking(db, booking)
    other = (
        musician_user
        if current_user.role == UserRole.contractor
        else contractor_user
    )
    notify_booking_completed(db, other, str(booking.id))

    db.commit()
    db.refresh(booking)
    return booking


@router.get("/{booking_id}/balance-due")
def get_balance_due(
    booking_id: str,
    current_user: User = Depends(deps.get_current_user),
    db: Session = Depends(deps.get_db),
):
    booking = get_booking_or_404(db, booking_id)
    assert_booking_viewer(db, booking, current_user)
    due = remaining_balance(db, booking)
    paid = retained_paid_total(db, booking.id)
    from app.services.platform_payment import (
        contractor_advance_due,
        contractor_payable_total,
        contractor_remaining_after_advance,
    )

    payable = contractor_payable_total(booking)
    fee = (
        float(booking.platform_fee_amount)
        if booking.platform_fee_amount is not None
        else 0.0
    )
    return {
        "booking_id": str(booking.id),
        "price_agreed": float(booking.price_agreed) if booking.price_agreed else None,
        "platform_fee_percent": (
            float(booking.platform_fee_percent)
            if booking.platform_fee_percent is not None
            else None
        ),
        "platform_fee_amount": fee if booking.price_agreed else None,
        "contractor_total": float(payable) if payable is not None else None,
        "suggested_advance": (
            float(contractor_advance_due(booking) or 0)
            if booking.price_agreed is not None
            else None
        ),
        "suggested_remaining": (
            float(contractor_remaining_after_advance(booking) or 0)
            if booking.price_agreed is not None
            else None
        ),
        "balance_due": float(due),
        "amount_paid": float(paid),
        "currency": "PEN",
    }
