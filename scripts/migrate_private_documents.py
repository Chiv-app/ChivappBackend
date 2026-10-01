import argparse
import logging
import mimetypes
import os
import sys
import uuid
from urllib.parse import urlparse

# Ensure app is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload
from app.db.session import SessionLocal
from app.models.contractor_profile import ContractorProfile
from app.models.musician_profile import MusicianProfile
from app.models.contract import Contract
from app.models.booking import Booking
from app.models.booking_complaint import BookingComplaint
from app.models.uploaded_file import UploadedFile
from app.core.config import settings
from app.services.uploads import (
    PRIVATE_URL_PREFIX,
    PUBLIC_URL_PREFIX,
    GCS_PUBLIC_PREFIX,
    read_upload_bytes,
    _store_private,
    sniff_content_type,
    ALLOWED_TYPES,
)

logger = logging.getLogger("migrate_private_documents")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(handler)


def get_owner_for_contractor_signature(db: Session, contract: Contract):
    booking = db.query(Booking).filter(Booking.contract_id == contract.id).first()
    return booking.contractor_user_id if booking else None


def get_owner_for_musician_signature(db: Session, contract: Contract):
    booking = db.query(Booking).filter(Booking.contract_id == contract.id).first()
    if not booking:
        return None
    musician_profile = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
    return musician_profile.user_id if musician_profile else None


def get_owner_for_musician_evidence(db: Session, complaint: BookingComplaint):
    booking = db.query(Booking).filter(Booking.id == complaint.booking_id).first()
    if not booking:
        return None
    musician_profile = db.query(MusicianProfile).filter(MusicianProfile.id == booking.musician_id).first()
    return musician_profile.user_id if musician_profile else None


def migrate_url(db: Session, record, field_name: str, owner_user_id, execute: bool):
    url = getattr(record, field_name)
    
    # Ignore empty or already private URLs
    if not url or url.startswith(PRIVATE_URL_PREFIX):
        return

    # Check if it's a known public pattern
    if not (url.startswith(PUBLIC_URL_PREFIX) or url.startswith(GCS_PUBLIC_PREFIX)):
        logger.warning(f"URL {url} doesn't look like a standard upload. Skipping.")
        return

    logger.info(f"[{record.__class__.__name__}.{field_name}] Found public URL: {url}")
    
    if not owner_user_id:
        logger.error(f"Cannot determine owner for {record.__class__.__name__} ID {record.id}. Skipping.")
        return

    # Read bytes
    content = read_upload_bytes(url)
    if not content:
        logger.error(f"Could not read bytes for {url}. File might be missing.")
        return

    content_type = sniff_content_type(content[:16])
    if not content_type:
        # Fallback to extension
        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if ext == '.pdf':
            content_type = 'application/pdf'
        elif ext in ['.jpg', '.jpeg']:
            content_type = 'image/jpeg'
        elif ext == '.png':
            content_type = 'image/png'
        else:
            logger.error(f"Unknown content type for {url}. Skipping.")
            return

    ext = ALLOWED_TYPES.get(content_type, '.bin')
    if ext == '.bin':
        logger.error(f"Unsupported content type {content_type} for {url}. Skipping.")
        return

    new_filename = f"{uuid.uuid4().hex}{ext}"
    new_url = f"{PRIVATE_URL_PREFIX}{new_filename}"

    logger.info(f" -> Will migrate to {new_url} owned by {owner_user_id}")

    if execute:
        try:
            # 1. Store the file privately
            _store_private(new_filename, content, content_type)
            
            # 2. Add UploadedFile record
            uploaded_file = UploadedFile(
                filename=new_filename,
                owner_user_id=owner_user_id,
                content_type=content_type,
                size_bytes=len(content)
            )
            db.add(uploaded_file)
            
            # 3. Update the record
            setattr(record, field_name, new_url)
            db.commit()
            logger.info(" -> Success.")
            
            # Note: We are deliberately NOT deleting the old public file yet.
            # It's safer to delete them in a separate pass or manually after verifying.
        except Exception as e:
            db.rollback()
            logger.exception(f" -> Failed to migrate {url}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Migrate legacy public sensitive documents to the private bucket.")
    parser.add_argument("--execute", action="store_true", help="Actually perform the migration and update the DB.")
    args = parser.parse_args()

    if not args.execute:
        logger.info("=== DRY RUN MODE: No changes will be saved ===")
    else:
        logger.info("=== EXECUTE MODE: Changes will be written to GCS/Disk and Database ===")
        
    db = SessionLocal()
    try:
        # 1. ContractorProfile.id_document_url
        contractors = db.query(ContractorProfile).filter(ContractorProfile.id_document_url.isnot(None)).all()
        for cp in contractors:
            migrate_url(db, cp, "id_document_url", cp.user_id, args.execute)
            
        # 2. MusicianProfile.id_document_url
        musicians = db.query(MusicianProfile).filter(MusicianProfile.id_document_url.isnot(None)).all()
        for mp in musicians:
            migrate_url(db, mp, "id_document_url", mp.user_id, args.execute)
            
        # 3. Contract Signatures
        contracts = db.query(Contract).all()
        for contract in contracts:
            if contract.contractor_signature_url:
                owner = get_owner_for_contractor_signature(db, contract)
                migrate_url(db, contract, "contractor_signature_url", owner, args.execute)
            if contract.musician_signature_url:
                owner = get_owner_for_musician_signature(db, contract)
                migrate_url(db, contract, "musician_signature_url", owner, args.execute)
                
        # 4. Booking Complaints
        complaints = db.query(BookingComplaint).all()
        for complaint in complaints:
            if complaint.evidence_url:
                migrate_url(db, complaint, "evidence_url", complaint.opened_by_user_id, args.execute)
            if complaint.musician_response_evidence_url:
                owner = get_owner_for_musician_evidence(db, complaint)
                migrate_url(db, complaint, "musician_response_evidence_url", owner, args.execute)
            if complaint.refund_evidence_url:
                migrate_url(db, complaint, "refund_evidence_url", complaint.refund_sent_by_admin_id, args.execute)

        logger.info("Migration completed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
