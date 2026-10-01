import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class UploadedFile(Base):
    """Registro de archivos privados (DNI, firmas, comprobantes) para controlar
    quién puede descargarlos. Los archivos públicos (fotos, media) no se registran."""

    __tablename__ = "uploaded_file"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nombre del archivo (p. ej. "3f1c...e2.pdf"); único y no adivinable.
    filename = Column(String, nullable=False, unique=True, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("user.id"), nullable=False, index=True)
    content_type = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
