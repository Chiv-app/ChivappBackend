"""Tipos de columna compartidos."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """`TIMESTAMP WITH TIME ZONE` en la base de datos.

    La aplicación sigue trabajando con datetimes UTC *sin zona* (`datetime.utcnow()`), así que
    este tipo convierte en los dos sentidos:

    - Al guardar: un valor sin zona se interpreta como UTC y uno con zona se pasa a UTC. Así el
      valor guardado no depende de la zona horaria de la sesión de PostgreSQL (la app usa
      `America/Lima`).
    - Al leer: devuelve UTC sin zona, igual que antes del cambio de tipo.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
