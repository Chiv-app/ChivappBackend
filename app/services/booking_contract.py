from datetime import datetime

from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingStatus
from app.models.contract import Contract
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.user import User
from app.services.contract_pdf import build_booking_contract_snapshot


def create_booking_contract(
    db: Session,
    booking: Booking,
    musician: MusicianProfile,
    musician_user: User,
    contractor: ContractorProfile,
    contractor_user: User,
) -> Contract:
    title, body, context = build_booking_contract_snapshot(
        booking,
        musician,
        musician_user,
        contractor,
        contractor_user,
    )

    existing = db.query(Contract).filter(Contract.booking_id == booking.id).first()
    if existing:
        if not existing.contractor_signed:
            existing.title = title
            existing.body = body
            existing.context = context
        if not existing.musician_signature_url and musician.signature_image_url:
            existing.musician_signature_url = musician.signature_image_url
        return existing

    now = datetime.utcnow()
    sig_url = (musician.signature_image_url or "").strip() or None
    contract = Contract(
        booking_id=booking.id,
        title=title,
        body=body,
        context=context,
        musician_signed=True,
        musician_sign_timestamp=now,
        musician_signature_url=sig_url,
    )
    if booking.status in (BookingStatus.requested, BookingStatus.accepted):
        booking.status = BookingStatus.contract_pending
    db.add(contract)
    return contract
