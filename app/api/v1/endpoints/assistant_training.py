"""Entrenamiento del asistente IA: conocimiento, dudas pendientes, versiones, preparación y vista previa.

Todo es del músico dueño del perfil y no llama al modelo, así que no consume cupo de IA.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api import deps
from app.api.v1.endpoints.assistant import _assert_musician_user, _resolve_target_profile
from app.models.musician_assistant import (
    AssistantKnowledgeEntry,
    AssistantPendingQuestion,
)
from app.models.user import User
from app.schemas.assistant import (
    ConfigVersionOut,
    KnowledgeEntryIn,
    KnowledgeEntryOut,
    PendingAnswerIn,
    PendingQuestionOut,
    PreviewCompareIn,
    PreviewQuoteIn,
    PreviewQuoteOut,
    PreviewScenarioOut,
    ReadinessOut,
)
from app.services import assistant_training as training
from app.services.musician_assistant import get_or_create_assistant_config, get_or_create_whatsapp_channel

router = APIRouter(tags=["Musician AI Assistant Training"])

CONTRACT_TOPIC_MESSAGE = (
    "Eso ya lo define tu contrata y la política de cancelación de Chivapp; el asistente lo responde desde ahí. "
    "Si quieres cambiarlo, edita tu contrata."
)
MAX_ENTRIES_PER_PROFILE = 100


def _own_entry(db: Session, profile_id, entry_id: UUID) -> AssistantKnowledgeEntry:
    entry = (
        db.query(AssistantKnowledgeEntry)
        .filter(AssistantKnowledgeEntry.id == entry_id, AssistantKnowledgeEntry.profile_id == profile_id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Respuesta no encontrada.")
    return entry


def _own_pending(db: Session, profile_id, pending_id: UUID) -> AssistantPendingQuestion:
    pending = (
        db.query(AssistantPendingQuestion)
        .filter(
            AssistantPendingQuestion.id == pending_id,
            AssistantPendingQuestion.profile_id == profile_id,
            AssistantPendingQuestion.status == "open",
        )
        .first()
    )
    if pending is None:
        raise HTTPException(status_code=404, detail="Duda no encontrada o ya resuelta.")
    return pending


# --- Conocimiento ---


@router.get("/assistant/knowledge", response_model=list[KnowledgeEntryOut])
def list_knowledge(
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    return training.list_entries(db, profile.id)


@router.post("/assistant/knowledge", response_model=KnowledgeEntryOut, status_code=201)
def create_knowledge(
    payload: KnowledgeEntryIn,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    if training.is_contract_topic(payload.question):
        raise HTTPException(status_code=422, detail=CONTRACT_TOPIC_MESSAGE)
    total = db.query(func.count(AssistantKnowledgeEntry.id)).filter(AssistantKnowledgeEntry.profile_id == profile.id).scalar()
    if (total or 0) >= MAX_ENTRIES_PER_PROFILE:
        raise HTTPException(status_code=422, detail=f"Llegaste al máximo de {MAX_ENTRIES_PER_PROFILE} respuestas.")
    entry = training.create_entry(db, profile, payload.question, payload.answer, payload.source)
    db.commit()
    db.refresh(entry)
    return entry


@router.put("/assistant/knowledge/{entry_id}", response_model=KnowledgeEntryOut)
def update_knowledge(
    entry_id: UUID,
    payload: KnowledgeEntryIn,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    entry = _own_entry(db, profile.id, entry_id)
    if training.is_contract_topic(payload.question):
        raise HTTPException(status_code=422, detail=CONTRACT_TOPIC_MESSAGE)
    entry.question = payload.question.strip()[:300]
    entry.answer = payload.answer.strip()
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/assistant/knowledge/{entry_id}", status_code=204)
def delete_knowledge(
    entry_id: UUID,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    db.delete(_own_entry(db, profile.id, entry_id))
    db.commit()
    return Response(status_code=204)


# --- Dudas pendientes ---


@router.get("/assistant/pending", response_model=list[PendingQuestionOut])
def list_pending(
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    return training.list_open_pending(db, profile.id)


@router.post("/assistant/pending/{pending_id}/answer", response_model=KnowledgeEntryOut, status_code=201)
def answer_pending(
    pending_id: UUID,
    payload: PendingAnswerIn,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Responde una duda: la respuesta queda como conocimiento y la duda se cierra."""
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    pending = _own_pending(db, profile.id, pending_id)
    if training.is_contract_topic(pending.question):
        raise HTTPException(status_code=422, detail=CONTRACT_TOPIC_MESSAGE)
    entry = training.answer_pending(db, profile, pending, payload.answer)
    db.commit()
    db.refresh(entry)
    return entry


@router.post("/assistant/pending/{pending_id}/dismiss", status_code=204)
def dismiss_pending(
    pending_id: UUID,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    _own_pending(db, profile.id, pending_id).status = "dismissed"
    db.commit()
    return Response(status_code=204)


# --- Versiones ---


@router.get("/assistant/versions", response_model=list[ConfigVersionOut])
def list_versions(
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    config = get_or_create_assistant_config(db, profile)
    # Si todavía no hay historial, la configuración actual se guarda como versión inicial.
    if training.record_version(db, profile, config) is not None:
        db.commit()
    current = training.snapshot_config(config)
    return [
        ConfigVersionOut(
            number=v.number,
            label=v.label,
            created_at=v.created_at,
            package_count=len((v.snapshot or {}).get("packages") or []),
            tone=(v.snapshot or {}).get("tone"),
            is_current=(v.snapshot == current),
        )
        for v in training.list_versions(db, profile.id)
    ]


@router.post("/assistant/versions/{number}/restore", response_model=ConfigVersionOut)
def restore_version(
    number: int,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    config = get_or_create_assistant_config(db, profile)
    version = training.restore_version(db, profile, config, number)
    if version is None:
        raise HTTPException(status_code=404, detail="Versión no encontrada.")
    db.commit()
    return ConfigVersionOut(
        number=version.number,
        label=version.label,
        created_at=version.created_at,
        package_count=len((version.snapshot or {}).get("packages") or []),
        tone=(version.snapshot or {}).get("tone"),
        is_current=True,
    )


# --- Preparación y vista previa ---


@router.get("/assistant/readiness", response_model=ReadinessOut)
def get_readiness(
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    config = get_or_create_assistant_config(db, profile)
    channel = get_or_create_whatsapp_channel(db, current_user)
    return training.compute_readiness(db, profile, config, channel)


@router.post("/assistant/preview/quote", response_model=PreviewQuoteOut)
def preview_quote(
    payload: PreviewQuoteIn,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Cotiza un caso con las reglas que se están editando, sin guardarlas."""
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    saved = get_or_create_assistant_config(db, profile)
    config = training.config_like(profile, payload.config, saved)
    return training.preview_quote(profile, config, payload)


@router.post("/assistant/preview/compare", response_model=list[PreviewScenarioOut])
def preview_compare(
    payload: PreviewCompareIn,
    profile_id: UUID | None = Query(default=None),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Casos típicos cotizados con lo guardado y con lo que se está editando, para ver qué cambiaría."""
    _assert_musician_user(current_user)
    profile = _resolve_target_profile(db, current_user, profile_id)
    saved = get_or_create_assistant_config(db, profile)
    proposed = training.config_like(profile, payload.config, saved)
    return training.preview_compare(profile, saved, proposed)
