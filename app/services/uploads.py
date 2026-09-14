import uuid
import logging
from pathlib import Path

from fastapi import UploadFile
from app.core.config import settings

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parents[2] / "uploads"
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/pjpeg",
    "image/png",
    "image/x-png",
    "image/webp",
    "image/gif",
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
}
ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".pdf",
}
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB


def ensure_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


async def save_upload(file: UploadFile) -> str:
    raw_ct = (file.content_type or "").split(";")[0].strip().lower()
    extension = Path(file.filename or "file").suffix.lower()

    # Si el content_type es conocido o si es genérico pero la extensión es válida
    is_valid_mime = raw_ct in ALLOWED_CONTENT_TYPES
    is_valid_ext = extension in ALLOWED_EXTENSIONS

    if not is_valid_mime and not is_valid_ext:
        raise ValueError("Tipo de archivo no permitido. Formatos aceptados: JPG, PNG, WEBP o PDF.")

    if not extension:
        if raw_ct.startswith("image/"):
            extension = ".jpg"
        elif "pdf" in raw_ct:
            extension = ".pdf"
        else:
            extension = ".jpg"

    content = await file.read()
    if not content:
        raise ValueError("El archivo seleccionado está vacío.")

    if len(content) > MAX_FILE_SIZE:
        raise ValueError("El archivo supera el tamaño máximo permitido (15 MB).")

    filename = f"{uuid.uuid4()}{extension}"
    
    if settings.GCP_BUCKET_NAME:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(settings.GCP_BUCKET_NAME)
            blob = bucket.blob(filename)
            # Upload from memory
            blob.upload_from_string(content, content_type=raw_ct or "application/octet-stream")
            return f"https://storage.googleapis.com/{settings.GCP_BUCKET_NAME}/{filename}"
        except Exception as e:
            logger.error(f"Failed to upload to GCS: {e}")
            raise ValueError(f"Error al subir el archivo a la nube: {e}")
            
    # Fallback to local storage (e.g. local development)
    destination = ensure_upload_dir() / filename

    with destination.open("wb") as buffer:
        buffer.write(content)

    return f"/uploads/{filename}"
