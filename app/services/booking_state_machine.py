from enum import Enum
from typing import List, Callable, Dict, Any, Optional
from fastapi import HTTPException
from app.models.booking import BookingStatus
from app.models.user import UserRole

class BookingAction(str, Enum):
    UPDATE_REQUEST = "UPDATE_REQUEST"
    REJECT_REQUEST = "REJECT_REQUEST"
    QUOTE = "QUOTE"
    REJECT_QUOTE = "REJECT_QUOTE"
    ACCEPT_QUOTE = "ACCEPT_QUOTE"
    REOPEN_QUOTE = "REOPEN_QUOTE"
    SIGN_CONTRACT = "SIGN_CONTRACT"
    VIEW_CONTRACT = "VIEW_CONTRACT"
    CANCEL = "CANCEL"
    REQUEST_CHANGE = "REQUEST_CHANGE"
    ACCEPT_CHANGE = "ACCEPT_CHANGE"
    REJECT_CHANGE = "REJECT_CHANGE"
    MARK_IN_PROGRESS = "MARK_IN_PROGRESS"
    COMPLETE = "COMPLETE"
    CHAT_MESSAGE = "CHAT_MESSAGE"
    ADD_REVIEW = "ADD_REVIEW"
    ADD_COMPLAINT = "ADD_COMPLAINT"
    RESPOND_COMPLAINT = "RESPOND_COMPLAINT"

class TransitionRule:
    def __init__(
        self,
        from_statuses: List[BookingStatus],
        allowed_roles: List[UserRole],
        error_message: str
    ):
        self.from_statuses = from_statuses
        self.allowed_roles = allowed_roles
        self.error_message = error_message

# We define the valid "from" statuses for each logical action a user can take.
# Instead of hardcoding `if booking.status not in (...)` in every endpoint.
ACTION_RULES: Dict[BookingAction, TransitionRule] = {
    BookingAction.UPDATE_REQUEST: TransitionRule(
        from_statuses=[BookingStatus.requested],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes editar solicitudes en estado 'requested'"
    ),
    BookingAction.REJECT_REQUEST: TransitionRule(
        from_statuses=[BookingStatus.requested],
        allowed_roles=[UserRole.musician],
        error_message="Solo puedes rechazar solicitudes en estado 'requested'"
    ),
    BookingAction.QUOTE: TransitionRule(
        from_statuses=[BookingStatus.requested, BookingStatus.accepted],
        allowed_roles=[UserRole.musician],
        error_message="Solo puedes cotizar solicitudes pendientes o actualizar cotizaciones aǧn no aceptadas"
    ),
    BookingAction.REJECT_QUOTE: TransitionRule(
        from_statuses=[BookingStatus.accepted],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes rechazar cotizaciones en estado 'accepted'"
    ),
    BookingAction.REOPEN_QUOTE: TransitionRule(
        from_statuses=[BookingStatus.accepted, BookingStatus.contract_pending],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes editar y reabrir cotizacion en estados accepted o contract_pending"
    ),
    BookingAction.ACCEPT_QUOTE: TransitionRule(
        from_statuses=[BookingStatus.accepted],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes aceptar cotizaciones en estado 'accepted'"
    ),
    BookingAction.SIGN_CONTRACT: TransitionRule(
        from_statuses=[BookingStatus.accepted, BookingStatus.contract_pending],
        allowed_roles=[UserRole.contractor], # Actually only contractor signs and triggers payment. Musician signs templates earlier.
        error_message="Solo puedes firmar el contrato cuando la cotizacion fue aceptada o el contrato esta pendiente"
    ),
    BookingAction.VIEW_CONTRACT: TransitionRule(
        from_statuses=[BookingStatus.accepted, BookingStatus.contract_pending],
        allowed_roles=[UserRole.contractor, UserRole.musician, UserRole.admin],
        error_message="No hay contrato disponible en este estado de la reserva"
    ),
    BookingAction.CANCEL: TransitionRule(
        from_statuses=[
            BookingStatus.requested,
            BookingStatus.accepted,
            BookingStatus.contract_pending,
            BookingStatus.contract_signed,
            BookingStatus.payment_pending,
            BookingStatus.payment_retained,
            BookingStatus.change_pending,
            BookingStatus.balance_pending,
            BookingStatus.balance_review,
        ],
        allowed_roles=[UserRole.contractor, UserRole.musician, UserRole.admin],
        error_message="No se puede cancelar en este estado"
    ),
    BookingAction.REQUEST_CHANGE: TransitionRule(
        from_statuses=[
            BookingStatus.payment_retained,
            BookingStatus.balance_pending,
            BookingStatus.balance_review
        ],
        allowed_roles=[UserRole.contractor, UserRole.musician],
        error_message="Solo se pueden solicitar cambios en reservas ya confirmadas"
    ),
    BookingAction.ACCEPT_CHANGE: TransitionRule(
        from_statuses=[BookingStatus.change_pending],
        allowed_roles=[UserRole.contractor, UserRole.musician],
        error_message="No hay cambios pendientes por aceptar"
    ),
    BookingAction.REJECT_CHANGE: TransitionRule(
        from_statuses=[BookingStatus.change_pending],
        allowed_roles=[UserRole.contractor, UserRole.musician],
        error_message="No hay cambios pendientes por rechazar"
    ),
    BookingAction.MARK_IN_PROGRESS: TransitionRule(
        from_statuses=[
            BookingStatus.payment_retained,
            BookingStatus.balance_pending,
            BookingStatus.balance_review
        ],
        allowed_roles=[UserRole.admin, UserRole.musician], # Usually admin or cron
        error_message="La reserva no se puede marcar en progreso desde su estado actual"
    ),
    BookingAction.COMPLETE: TransitionRule(
        from_statuses=[
            BookingStatus.in_progress,
            BookingStatus.payment_retained,
            BookingStatus.balance_pending,
            BookingStatus.balance_review
        ],
        allowed_roles=[UserRole.admin, UserRole.contractor],
        error_message="La reserva no esta en curso ni confirmada como para finalizarla"
    )
    BookingAction.CHAT_MESSAGE: TransitionRule(
        from_statuses=[
            BookingStatus.requested,
            BookingStatus.accepted,
            BookingStatus.contract_pending,
            BookingStatus.contract_signed,
            BookingStatus.payment_pending,
            BookingStatus.payment_retained,
            BookingStatus.change_pending,
            BookingStatus.balance_pending,
            BookingStatus.balance_review,
            BookingStatus.in_progress,
            BookingStatus.payment_released
        ],
        allowed_roles=[UserRole.contractor, UserRole.musician],
        error_message="La conversacion no esta disponible en reservas finalizadas o canceladas"
    ),
    BookingAction.ADD_REVIEW: TransitionRule(
        from_statuses=[
            BookingStatus.in_progress,
            BookingStatus.payment_released,
            BookingStatus.payment_retained,
            BookingStatus.balance_pending,
            BookingStatus.balance_review
        ],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes reseñar el evento despues de confirmado o finalizado"
    ),
    BookingAction.ADD_COMPLAINT: TransitionRule(
        from_statuses=[
            BookingStatus.in_progress,
            BookingStatus.payment_released,
            BookingStatus.payment_retained,
            BookingStatus.balance_pending,
            BookingStatus.balance_review
        ],
        allowed_roles=[UserRole.contractor],
        error_message="Solo puedes reportar un problema si el evento ya empezo o esta por empezar"
    ),
    BookingAction.RESPOND_COMPLAINT: TransitionRule(
        from_statuses=[
            BookingStatus.in_progress,
            BookingStatus.payment_released,
            BookingStatus.completed
        ],
        allowed_roles=[UserRole.musician],
        error_message="Solo puedes responder una queja si la reserva ya inicio o termino"
    )
}

def assert_can_execute_action(booking, action: BookingAction, current_user=None):
    """
    Evalǧa si la reserva esta en un estado vǭlido para la accion solicitada
    y opcionalmente si el rol del usuario puede ejecutarla.
    Lanza HTTPException(400) si no cumple.
    """
    rule = ACTION_RULES.get(action)
    if not rule:
        raise ValueError(f"Action {action} is not defined in state machine")
    
    if booking.status not in rule.from_statuses:
        raise HTTPException(
            status_code=400,
            detail=rule.error_message
        )
    
    if current_user and current_user.role not in rule.allowed_roles:
        raise HTTPException(
            status_code=403,
            detail="No tienes permiso para realizar esta accion en este estado"
        )
