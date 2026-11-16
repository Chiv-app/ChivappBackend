"""Búsqueda semántica del asistente con pgvector.

Los embeddings se sustituyen por unos deterministas (bolsa de palabras), así que no llaman al modelo ni cuestan.
Sin la extensión pgvector las pruebas se omiten; en CI (`REQUIRE_PGVECTOR=1`) fallan, para que nadie se entere
tarde de que la imagen de Postgres dejó de traerla.
"""

import hashlib
import math
import os
import re
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import app.services.assistant_knowledge as knowledge
from app.core.config import settings
from app.models.musician_assistant import MusicianAssistantConfig
from app.models.musician_profile import AvailabilityType, MusicianProfile
from app.models.profile_status import ProfileStatus
from app.models.user import User, UserRole

pytestmark = pytest.mark.db


def _fake_embedding(value: str) -> list[float]:
    vector = [0.0] * knowledge.EMBED_DIMENSIONS
    for word in re.findall(r"[a-záéíóúñ]{4,}", value.lower()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % knowledge.EMBED_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


@pytest.fixture
def fake_embeddings(monkeypatch):
    calls = {"texts": 0}

    def fake(texts, task_type="RETRIEVAL_DOCUMENT"):
        calls["texts"] += len(texts)
        return [_fake_embedding(t) for t in texts]

    monkeypatch.setattr(knowledge, "embed_texts", fake)
    return calls


@pytest.fixture
def pgvector(db_session):
    if not knowledge.vector_available(db_session):
        if os.environ.get("REQUIRE_PGVECTOR") == "1":
            pytest.fail("REQUIRE_PGVECTOR=1 pero pgvector no está activo en la base de pruebas")
        pytest.skip("pgvector no está disponible en esta base de datos")


def _profile(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"k_{suffix}@test.com",
        fullname="Carlos",
        role=UserRole.musician,
        is_active=True,
        is_verified=True,
        email_verified_at=datetime.utcnow(),
    )
    db_session.add(user)
    db_session.commit()
    profile = MusicianProfile(
        user_id=user.id,
        is_primary=True,
        stage_name=f"Grupo {suffix}",
        slug=f"grupo-{suffix}",
        status=ProfileStatus.published,
        availability_type=AvailabilityType.both,
        location_city="Lima",
        bio="Más de diez años animando celebraciones.",
        genres=["Boleros"],
        details={"includes": ["sound"]},
    )
    db_session.add(profile)
    db_session.commit()
    config = MusicianAssistantConfig(
        profile_id=profile.id,
        enabled=True,
        packages=[{"id": "a", "name": "Show estándar", "description": "12 canciones", "members_count": 4, "duration_hours": 1, "price": 450}],
    )
    db_session.add(config)
    db_session.commit()
    return profile, config


def _count(db_session, profile) -> int:
    return db_session.execute(
        text("SELECT count(*) FROM assistant_knowledge_chunk WHERE profile_id = :p"), {"p": str(profile.id)}
    ).scalar()


def test_pgvector_is_active_when_ci_requires_it(db_session):
    if os.environ.get("REQUIRE_PGVECTOR") != "1":
        pytest.skip("solo se exige en CI")
    assert knowledge.vector_available(db_session)


def test_sync_is_idempotent_and_only_embeds_what_changed(db_session, pgvector, fake_embeddings):
    profile, config = _profile(db_session)

    first = knowledge.sync_profile_chunks(db_session, profile, config)
    embedded_first = fake_embeddings["texts"]
    second = knowledge.sync_profile_chunks(db_session, profile, config)

    assert first["added"] > 0 and first["removed"] == 0
    assert second["added"] == 0 and second["removed"] == 0
    assert fake_embeddings["texts"] == embedded_first
    assert _count(db_session, profile) == first["added"]

    profile.contract_template_body = "<h3>Mis condiciones</h3><p>Trabajamos solo con reserva confirmada por Chivapp y avisamos cualquier cambio.</p>"
    db_session.commit()
    third = knowledge.sync_profile_chunks(db_session, profile, config)

    assert third["added"] >= 1 and third["removed"] >= 1
    assert fake_embeddings["texts"] > embedded_first


def test_search_finds_the_cancellation_policy_first(db_session, pgvector, fake_embeddings):
    profile, config = _profile(db_session)

    found, mode = knowledge.search_knowledge(
        db_session, profile, config, "¿Qué pasa con el reembolso si cancelo la reserva con pocos días?", min_score=0.05
    )

    assert mode == "vector"
    assert found[0]["source"] in {"policy", "contract"}
    assert "reembols" in found[0]["content"].lower()
    assert found[0]["score"] >= found[-1]["score"]


def test_search_is_scoped_to_the_profile(db_session, pgvector, fake_embeddings):
    profile, config = _profile(db_session)
    other, other_config = _profile(db_session)
    knowledge.sync_profile_chunks(db_session, other, other_config)

    found, _mode = knowledge.search_knowledge(db_session, profile, config, "cancelación reembolso", min_score=0.0, k=50)

    assert found
    ids = db_session.execute(
        text("SELECT DISTINCT profile_id FROM assistant_knowledge_chunk")
    ).scalars().all()
    assert {str(i) for i in ids} == {str(profile.id), str(other.id)}
    assert _count(db_session, profile) == len(found) or len(found) <= _count(db_session, profile)


def test_search_below_the_threshold_returns_empty(db_session, pgvector, fake_embeddings):
    profile, config = _profile(db_session)

    found, mode = knowledge.search_knowledge(db_session, profile, config, "zzzz qqqq xxxx", min_score=0.9)

    assert found == [] and mode == "vacio"


def test_search_falls_back_to_all_the_knowledge_when_the_model_fails(db_session, pgvector, monkeypatch):
    profile, config = _profile(db_session)

    def boom(*_a, **_kw):
        raise knowledge.EmbeddingError("sin servicio")

    monkeypatch.setattr(knowledge, "embed_texts", boom)

    found, mode = knowledge.search_knowledge(db_session, profile, config, "¿Qué incluye el show?")

    assert mode == "todo" and found
    assert sum(len(f["content"]) for f in found) <= knowledge.MAX_CONTEXT_CHARS


def test_without_pgvector_the_search_uses_all_the_knowledge(db_session, monkeypatch):
    profile, config = _profile(db_session)
    monkeypatch.setattr(knowledge, "vector_available", lambda _db: False)

    found, mode = knowledge.search_knowledge(db_session, profile, config, "cancelación")

    assert mode == "todo" and found
    assert knowledge.sync_profile_chunks(db_session, profile, config) is None


def test_the_daily_embedding_cap_skips_indexing_instead_of_failing(db_session, pgvector, fake_embeddings, monkeypatch):
    profile, config = _profile(db_session)
    monkeypatch.setattr(settings, "ASSISTANT_EMBED_DAILY_LIMIT", 1)

    stats = knowledge.sync_profile_chunks(db_session, profile, config)

    assert stats["added"] == 0 and stats["skipped"] > 0
    assert fake_embeddings["texts"] == 0
    found, mode = knowledge.search_knowledge(db_session, profile, config, "cancelación")
    assert mode == "todo" and found
