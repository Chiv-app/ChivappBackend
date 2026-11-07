"""remove_orphan_booking_statuses

Los estados `payment_pending`, `balance_pending` y `balance_review` ya no se asignan en ningún
flujo (el pago es único y lo confirma el webhook de Mercado Pago). Esta migración mueve las
reservas que pudieran quedar en esos estados a su equivalente vigente:

- `balance_pending` / `balance_review` (flujo anterior de anticipo y saldo) -> `payment_retained`
- `payment_pending` (nunca se asignaba)                                      -> `contract_signed`

Las etiquetas siguen existiendo en el tipo ENUM de PostgreSQL (quitar valores de un ENUM exige
recrear el tipo y bloquear la tabla); el código de la aplicación ya no las usa.

Revision ID: a1b2c3d4e5f7
Revises: f0a1b2c3d4e5
Create Date: 2026-11-06 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE booking
        SET status = 'payment_retained'
        WHERE status::text IN ('balance_pending', 'balance_review')
        """
    )
    op.execute(
        """
        UPDATE booking
        SET status = 'contract_signed'
        WHERE status::text = 'payment_pending'
        """
    )


def downgrade() -> None:
    # Los estados antiguos no se restauran: eran inalcanzables y no hay forma de saber el original.
    raise RuntimeError("Downgrade no soportado; restaura desde copia de seguridad.")
