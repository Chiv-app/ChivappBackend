"""Subida y lectura de archivos.

- El tipo se detecta por el contenido (magic bytes), nunca por el nombre ni el
  Content-Type que envía el cliente, y la extensión la decide el servidor.
- Públicos (fotos, media): disco `uploads/` (servido como estático) o bucket
  GCS_BUCKET_NAME. URL: /uploads/<archivo> o https://storage.googleapis.com/...
- Privados (DNI, firmas, comprobantes): disco `uploads_private/` (NO servido
  como estático) o bucket privado GCP_PRIVATE_BUCKET_NAME. URL:
  /uploads/private/<archivo>, servida solo a usuarios autorizados.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.core.config import settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BASE_DIR / "uploads"
PRIVATE_UPLOAD_DIR = BASE_DIR / "uploads_private"

PRIVATE_URL_PREFIX = "/uploads/private/"
PUBLIC_URL_PREFIX = "/uploads/"
GCS_PUBLIC_PREFIX = "https://storage.googleapis.com/"

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
_CHUNK = 1024 * 1024

# Solo nombres generados por el servidor: uuid + extensión permitida.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")

ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}
EXTENSION_TYPES = {ext: ct for ct, ext in ALLOWED_TYPES.items()} | {".jpeg": "image/jpeg"}


def sniff_content_type(head: bytes) -> str | None:
    """Tipo real según los primeros bytes del archivo."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def ensure_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def ensure_private_upload_dir() -> Path:
    PRIVATE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return PRIVATE_UPLOAD_DIR


async def _read_limited(file: UploadFile) -> bytes:
    """Lee el archivo por partes y corta apenas supera el máximo."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE:
            raise ValueError("El archivo supera el tamaño máximo permitido (15 MB).")
        chunks.append(chunk)
    return b"".join(chunks)


def _gcs_bucket(name: str):
    from google.cloud import storage

    return storage.Client().bucket(name)


async def save_upload(file: UploadFile, *, private: bool = False, owner_id=None, db=None) -> str:
    """Valida y guarda un archivo. Devuelve su URL.

    Con `private=True` se exige `owner_id` y `db`, y se registra el dueño para
    controlar las descargas.
    """
    content = await _read_limited(file)
    if not content:
        raise ValueError("El archivo seleccionado está vacío.")

    content_type = sniff_content_type(content[:16])
    if content_type not in ALLOWED_TYPES:
        raise ValueError("Tipo de archivo no permitido. Formatos aceptados: JPG, PNG, WEBP, GIF o PDF.")

    filename = f"{uuid.uuid4().hex}{ALLOWED_TYPES[content_type]}"

    if private:
        if owner_id is None or db is None:
            raise ValueError("Falta el dueño del archivo privado.")
        _store_private(filename, content, content_type)
        from app.models.uploaded_file import UploadedFile

        db.add(
            UploadedFile(
                filename=filename,
                owner_user_id=owner_id,
                content_type=content_type,
                size_bytes=len(content),
            )
        )
        db.commit()
        return f"{PRIVATE_URL_PREFIX}{filename}"

    if settings.GCP_BUCKET_NAME:
        try:
            blob = _gcs_bucket(settings.GCP_BUCKET_NAME).blob(filename)
            blob.upload_from_string(content, content_type=content_type)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al subir a GCS")
            raise ValueError("Error al subir el archivo. Intenta de nuevo.") from exc
        return f"{GCS_PUBLIC_PREFIX}{settings.GCP_BUCKET_NAME}/{filename}"

    (ensure_upload_dir() / filename).write_bytes(content)
    return f"{PUBLIC_URL_PREFIX}{filename}"


def _store_private(filename: str, content: bytes, content_type: str) -> None:
    if settings.GCP_PRIVATE_BUCKET_NAME:
        try:
            blob = _gcs_bucket(settings.GCP_PRIVATE_BUCKET_NAME).blob(filename)
            blob.upload_from_string(content, content_type=content_type)
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al subir archivo privado a GCS")
            raise ValueError("Error al subir el archivo. Intenta de nuevo.") from exc
    (ensure_private_upload_dir() / filename).write_bytes(content)


def _safe_name(name: str) -> str | None:
    if not name or name in {".", ".."} or not _SAFE_NAME.match(name):
        return None
    return name


def private_filename(url: str | None) -> str | None:
    if not url or not url.startswith(PRIVATE_URL_PREFIX):
        return None
    return _safe_name(url[len(PRIVATE_URL_PREFIX):])


def read_private_file(filename: str) -> bytes | None:
    name = _safe_name(filename)
    if not name:
        return None
    if settings.GCP_PRIVATE_BUCKET_NAME:
        try:
            blob = _gcs_bucket(settings.GCP_PRIVATE_BUCKET_NAME).blob(name)
            return blob.download_as_bytes() if blob.exists() else None
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo leer el archivo privado %s", name)
            return None
    path = PRIVATE_UPLOAD_DIR / name
    return path.read_bytes() if path.is_file() else None


def read_upload_bytes(url: str | None) -> bytes | None:
    """Lee cualquier archivo subido (público o privado, disco o GCS) a partir de su URL."""
    if not url:
        return None
    name = private_filename(url)
    if name:
        return read_private_file(name)

    if url.startswith(GCS_PUBLIC_PREFIX):
        bucket_and_name = url[len(GCS_PUBLIC_PREFIX):].split("/", 1)
        if len(bucket_and_name) != 2 or not settings.GCP_BUCKET_NAME:
            return None
        bucket, blob_name = bucket_and_name
        if bucket != settings.GCP_BUCKET_NAME or not _safe_name(blob_name):
            return None
        try:
            blob = _gcs_bucket(bucket).blob(blob_name)
            return blob.download_as_bytes() if blob.exists() else None
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo leer %s de GCS", blob_name)
            return None

    name = _safe_name(url.rsplit("/", 1)[-1])
    if not name or not url.startswith(PUBLIC_URL_PREFIX):
        return None
    path = UPLOAD_DIR / name
    return path.read_bytes() if path.is_file() else None


def upload_exists(url: str | None) -> bool:
    return read_upload_bytes(url) is not None


def assert_private_upload_owned(db, url: str | None, user_id, label: str = "archivo") -> None:
    """Exige que `url` sea un archivo privado subido por `user_id` (p. ej. su firma)."""
    from fastapi import HTTPException

    from app.models.uploaded_file import UploadedFile

    name = private_filename(url)
    record = (
        db.query(UploadedFile).filter(UploadedFile.filename == name).first() if name else None
    )
    if not record or str(record.owner_user_id) != str(user_id):
        raise HTTPException(400, f"El {label} no es válido. Vuelve a subirlo.")
