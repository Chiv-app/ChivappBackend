"""Agregados de reportes para el músico líder."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.models.booking import Booking, BookingReview, BookingStatus
from app.models.booking_complaint import BookingComplaint, BookingComplaintStatus
from app.models.ensemble_member import (
    BookingMemberPayout,
    BookingMemberPayoutStatus,
    EnsembleMember,
)
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.schemas.ensemble import (
    MusicianReportBookingRow,
    MusicianReportMemberRow,
    MusicianReportSeriesPoint,
    MusicianReportStatusSlice,
    MusicianReportsOut,
)
from app.services.ensemble_members import get_leader_musician_or_403
from app.services.platform_payment import musician_portion_of_paid

BOOKING_STATUS_LABELS = {
    BookingStatus.requested.value: "Solicitada",
    BookingStatus.accepted.value: "Cotizada",
    BookingStatus.contract_pending.value: "Contrato pendiente",
    BookingStatus.contract_signed.value: "Contrato firmado",
    BookingStatus.payment_pending.value: "Pago pendiente",
    BookingStatus.payment_retained.value: "Confirmada",
    BookingStatus.change_pending.value: "Cambio pendiente",
    # Legacy (unreachable) statuses: shown like a confirmed booking.
    BookingStatus.balance_pending.value: "Confirmada",
    BookingStatus.balance_review.value: "Confirmada",
    BookingStatus.in_progress.value: "En evento",
    BookingStatus.payment_released.value: "Pago liberado",
    BookingStatus.completed.value: "Finalizada",
    BookingStatus.cancelled.value: "Cancelada",
}

PAYOUT_STATUS_LABELS = {
    BookingMemberPayoutStatus.draft.value: "Borrador",
    BookingMemberPayoutStatus.locked.value: "Confirmado",
    BookingMemberPayoutStatus.paid.value: "Pagado",
}


def _parse_date(value: str | None, *, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"Fecha inválida: {value}") from exc


def _bucket_key(day: date, group_by: str) -> tuple[str, str]:
    if group_by == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
        return (
            start.isoformat(),
            f"{start.strftime('%d/%m')} – {end.strftime('%d/%m')}",
        )
    # month
    return (
        f"{day.year:04d}-{day.month:02d}",
        day.strftime("%b %Y"),
    )


def _iter_buckets(start: date, end: date, group_by: str) -> list[tuple[str, str]]:
    buckets: list[tuple[str, str]] = []
    if group_by == "week":
        cursor = start - timedelta(days=start.weekday())
        while cursor <= end:
            buckets.append(_bucket_key(cursor, "week"))
            cursor += timedelta(days=7)
        return buckets

    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        buckets.append(_bucket_key(cursor, "month"))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return buckets


def build_musician_reports(
    db: Session,
    current_user: User,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    group_by: str = "month",
    status: str | None = None,
) -> MusicianReportsOut:
    musician = get_leader_musician_or_403(db, current_user)
    today = date.today()
    period_to = _parse_date(date_to, fallback=today)
    period_from = _parse_date(
        date_from, fallback=period_to.replace(day=1) - timedelta(days=150)
    )
    if period_from > period_to:
        raise HTTPException(400, "El rango de fechas es inválido")
    if group_by not in {"month", "week"}:
        raise HTTPException(400, "group_by debe ser month o week")

    bookings_q = (
        db.query(Booking)
        .filter(
            Booking.musician_id == musician.id,
            Booking.event_date >= period_from,
            Booking.event_date <= period_to,
        )
        .order_by(Booking.event_date.asc())
    )
    if status:
        try:
            status_enum = BookingStatus(status)
        except ValueError as exc:
            raise HTTPException(400, f"Estado inválido: {status}") from exc
        bookings_q = bookings_q.filter(Booking.status == status_enum)

    bookings = bookings_q.all()
    booking_ids = [b.id for b in bookings]

    payments = (
        db.query(Payment)
        .filter(Payment.booking_id.in_(booking_ids))
        .all()
        if booking_ids
        else []
    )
    payouts = (
        db.query(BookingMemberPayout)
        .options(joinedload(BookingMemberPayout.ensemble_member))
        .filter(BookingMemberPayout.booking_id.in_(booking_ids))
        .all()
        if booking_ids
        else []
    )
    complaints = (
        db.query(BookingComplaint)
        .filter(BookingComplaint.booking_id.in_(booking_ids))
        .all()
        if booking_ids
        else []
    )
    final_reviews = (
        db.query(BookingReview)
        .filter(
            BookingReview.booking_id.in_(booking_ids),
            BookingReview.is_final.is_(True),
        )
        .all()
        if booking_ids
        else []
    )

    payments_by_booking: dict[UUID, list[Payment]] = defaultdict(list)
    for payment in payments:
        payments_by_booking[payment.booking_id].append(payment)

    payouts_by_booking: dict[UUID, list[BookingMemberPayout]] = defaultdict(list)
    for payout in payouts:
        payouts_by_booking[payout.booking_id].append(payout)

    bucket_defs = _iter_buckets(period_from, period_to, group_by)
    series_map: dict[str, MusicianReportSeriesPoint] = {
        key: MusicianReportSeriesPoint(bucket=key, label=label)
        for key, label in bucket_defs
    }

    status_counts: dict[str, int] = defaultdict(int)
    bookings_completed = 0
    bookings_cancelled = 0
    bookings_active = 0
    revenue_quoted = 0.0
    revenue_retained = 0.0
    revenue_released = 0.0
    member_assigned = 0.0
    member_paid = 0.0
    member_pending = 0.0

    member_agg: dict[UUID, MusicianReportMemberRow] = {}
    bookings_table: list[MusicianReportBookingRow] = []

    for booking in bookings:
        status_key = booking.status.value
        status_counts[status_key] += 1
        if booking.status == BookingStatus.completed:
            bookings_completed += 1
        elif booking.status == BookingStatus.cancelled:
            bookings_cancelled += 1
        elif booking.status != BookingStatus.requested:
            bookings_active += 1

        price = float(booking.price_agreed or 0)
        revenue_quoted += price

        retained_gross = sum(
            float(p.amount)
            for p in payments_by_booking.get(booking.id, [])
            if p.status == PaymentStatus.retained
        )
        released_gross = sum(
            float(p.amount)
            for p in payments_by_booking.get(booking.id, [])
            if p.status == PaymentStatus.released
        )
        retained = float(
            musician_portion_of_paid(booking, Decimal(str(retained_gross)))
        )
        released = float(
            musician_portion_of_paid(booking, Decimal(str(released_gross)))
        )
        revenue_retained += retained
        revenue_released += released

        assigned = 0.0
        paid = 0.0
        for payout in payouts_by_booking.get(booking.id, []):
            amount = float(payout.amount or 0)
            assigned += amount
            payout_status = (
                payout.status.value
                if hasattr(payout.status, "value")
                else str(payout.status)
            )
            if payout_status == BookingMemberPayoutStatus.paid.value:
                paid += amount
                member_paid += amount
            else:
                member_pending += amount

            member = payout.ensemble_member
            if not member:
                continue
            row = member_agg.get(member.id)
            if not row:
                row = MusicianReportMemberRow(
                    ensemble_member_id=member.id,
                    fullname=member.fullname,
                    email=str(member.email),
                    status=member.status.value
                    if hasattr(member.status, "value")
                    else str(member.status),
                    shows=0,
                    assigned=0,
                    paid=0,
                    pending=0,
                )
                member_agg[member.id] = row
            row.shows += 1
            row.assigned += amount
            if payout_status == BookingMemberPayoutStatus.paid.value:
                row.paid += amount
            else:
                row.pending += amount

        member_assigned += assigned

        bucket_key, _label = _bucket_key(booking.event_date, group_by)
        point = series_map.get(bucket_key)
        if point is None:
            point = MusicianReportSeriesPoint(bucket=bucket_key, label=bucket_key)
            series_map[bucket_key] = point
        point.bookings += 1
        point.revenue += price
        point.retained += retained
        point.released += released
        point.member_paid += paid

        bookings_table.append(
            MusicianReportBookingRow(
                booking_id=booking.id,
                event_type=booking.event_type,
                event_date=booking.event_date,
                location_city=booking.location_city,
                status=status_key,
                price_agreed=price if booking.price_agreed is not None else None,
                retained=retained,
                released=released,
                member_assigned=assigned,
                member_paid=paid,
            )
        )

    # Include members without payouts in period (zero rows) for completeness.
    all_members = (
        db.query(EnsembleMember)
        .filter(EnsembleMember.leader_user_id == current_user.id)
        .all()
    )
    for member in all_members:
        if member.id in member_agg:
            continue
        member_agg[member.id] = MusicianReportMemberRow(
            ensemble_member_id=member.id,
            fullname=member.fullname,
            email=str(member.email),
            status=member.status.value
            if hasattr(member.status, "value")
            else str(member.status),
            shows=0,
            assigned=0,
            paid=0,
            pending=0,
        )

    members_table = sorted(
        member_agg.values(),
        key=lambda row: (row.assigned, row.shows, row.fullname),
        reverse=True,
    )
    top_members = [row for row in members_table if row.assigned > 0][:8]

    payout_status_counts: dict[str, float] = defaultdict(float)
    for payout in payouts:
        key = (
            payout.status.value
            if hasattr(payout.status, "value")
            else str(payout.status)
        )
        payout_status_counts[key] += float(payout.amount or 0)

    complaints_open = 0
    complaints_settled = 0
    for complaint in complaints:
        if complaint.status == BookingComplaintStatus.settled:
            complaints_settled += 1
        else:
            complaints_open += 1

    rating_count = len(final_reviews)
    rating_avg = (
        round(sum(r.rating for r in final_reviews) / rating_count, 1)
        if rating_count
        else (
            float(musician.rating_avg)
            if getattr(musician, "rating_avg", None) is not None
            else None
        )
    )
    if not rating_count and getattr(musician, "rating_count", None):
        rating_count = int(musician.rating_count or 0)

    bookings_by_status = [
        MusicianReportStatusSlice(
            key=key,
            label=BOOKING_STATUS_LABELS.get(key, key),
            count=count,
        )
        for key, count in sorted(status_counts.items(), key=lambda x: -x[1])
    ]
    payouts_by_status = [
        MusicianReportStatusSlice(
            key=key,
            label=PAYOUT_STATUS_LABELS.get(key, key),
            count=0,
            value=value,
        )
        for key, value in sorted(
            payout_status_counts.items(), key=lambda x: -x[1]
        )
    ]

    series = [
        series_map[key]
        for key, _label in bucket_defs
        if key in series_map
    ]

    return MusicianReportsOut(
        period_from=period_from,
        period_to=period_to,
        group_by=group_by,
        bookings_total=len(bookings),
        bookings_completed=bookings_completed,
        bookings_cancelled=bookings_cancelled,
        bookings_active=bookings_active,
        revenue_quoted=round(revenue_quoted, 2),
        revenue_retained=round(revenue_retained, 2),
        revenue_released=round(revenue_released, 2),
        member_assigned=round(member_assigned, 2),
        member_paid=round(member_paid, 2),
        member_pending=round(member_pending, 2),
        rating_avg=rating_avg,
        rating_count=rating_count,
        complaints_total=len(complaints),
        complaints_open=complaints_open,
        complaints_settled=complaints_settled,
        series=series,
        bookings_by_status=bookings_by_status,
        payouts_by_status=payouts_by_status,
        top_members=top_members,
        bookings_table=list(reversed(bookings_table[-50:])),
        members_table=members_table,
    )
