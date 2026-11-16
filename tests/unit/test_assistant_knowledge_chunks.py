"""Fragmentos de conocimiento del asistente: no necesitan base de datos ni pgvector."""

from types import SimpleNamespace

from app.core.config import settings
from app.services.assistant_knowledge import build_chunks, contract_chunks, policy_chunk


def _profile(**overrides):
    data = dict(
        id="00000000-0000-0000-0000-000000000001",
        contract_template_body=None,
        bio="Más de diez años animando celebraciones.",
        genres=["Boleros"],
        repertoire=[{"title": "El Rey"}],
        songs=[],
        details={"includes": ["sound"], "setup": "30m"},
        showreel_video_url=None,
        videos=[],
        portfolio_description=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _config():
    return SimpleNamespace(
        packages=[
            {"id": "a", "name": "Show estándar", "description": "12 canciones", "members_count": 4, "duration_hours": 1, "price": 450}
        ],
        custom_instructions=None,
    )


def test_policy_chunk_uses_the_current_cancellation_settings(monkeypatch):
    monkeypatch.setattr(settings, "CANCEL_FULL_REFUND_MIN_DAYS", 20)
    monkeypatch.setattr(settings, "CANCEL_PARTIAL_REFUND_MIN_DAYS", 10)
    monkeypatch.setattr(settings, "CANCEL_PARTIAL_REFUND_PERCENT", 30)

    content = policy_chunk().content

    assert "20 días" in content and "entre 10 y 19 días, el 30 %" in content and "menos de 10 días" in content


def test_default_contract_is_split_by_sections_without_placeholders():
    chunks = contract_chunks(_profile())

    joined = " ".join(c.content for c in chunks)
    assert any("Honorarios y forma de pago" in c.content for c in chunks)
    assert any("Cancelaciones y reprogramación" in c.content for c in chunks)
    assert "{{" not in joined and "<h" not in joined
    assert all(len(c.content) <= 1200 for c in chunks)


def test_musician_contract_replaces_the_default_one():
    body = "<h3>Mis condiciones</h3><p>Trabajamos solo con reserva confirmada por Chivapp y avisamos cualquier cambio con tiempo.</p>"

    chunks = contract_chunks(_profile(contract_template_body=body))

    assert any("Mis condiciones" in c.content for c in chunks)
    assert not any("Honorarios y forma de pago" in c.content for c in chunks)


def test_package_chunks_never_carry_the_price():
    chunks = build_chunks(_profile(), _config())

    package = [c for c in chunks if c.source == "package"][0]
    assert "Show estándar" in package.content and "450" not in package.content


def test_chunks_are_deduplicated_and_capped(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_MAX_CHUNKS_PER_PROFILE", 5)

    chunks = build_chunks(_profile(), _config())

    assert len(chunks) == 5
    assert len({(c.source, c.ref, c.hash) for c in chunks}) == 5
