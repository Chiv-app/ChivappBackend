"""Conocimiento del asistente IA con búsqueda semántica (pgvector).

Por cada perfil se guardan fragmentos de texto con su embedding en `assistant_knowledge_chunk`:

- `contract`: las secciones de la contrata del músico (su plantilla, o la de Chivapp si no tiene una).
- `policy`: la política de cancelación de Chivapp, con los valores vigentes de la configuración.
- `profile`: biografía, géneros, repertorio, qué incluye el show.
- `package`: nombre y descripción de cada paquete (nunca el precio: el precio lo calcula el código).
- `faq`: las respuestas que el músico le enseñó (`assistant_knowledge_entry`).

La fuente de verdad sigue siendo el perfil y la contrata. Los fragmentos son una copia que se sincroniza de forma
perezosa: antes de buscar se compara el hash de cada fragmento y solo se calculan los embeddings de lo que cambió.
Sin tareas programadas.

Todo es tolerante: sin la extensión pgvector, sin clave, sin cupo de embeddings o con un fallo del modelo, la
búsqueda devuelve todo el conocimiento como contexto (con un tope de caracteres) y el asistente sigue funcionando.
Las operaciones vectoriales usan SQL directo: no hace falta el paquete `pgvector` de Python ni un tipo en el ORM.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from html import unescape
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.assistant_usage import consume_embeddings

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 768  # debe coincidir con `vector(768)` de la tabla
MAX_CHUNK_CHARS = 1200
MAX_CONTEXT_CHARS = 6000  # tope del contexto cuando se envía todo el conocimiento
DEFAULT_MIN_SCORE = 0.45
_EMBED_BATCH = 50

_TAG = re.compile(r"<[^>]+>")
_HEADING_SPLIT = re.compile(r"(?i)<h[1-3][^>]*>")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


class EmbeddingError(Exception):
    """No se pudo calcular un embedding (sin clave, error del servicio o respuesta inesperada)."""


@dataclass(frozen=True)
class Chunk:
    source: str
    ref: str
    content: str

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


# --- Fragmentos a partir de la contrata, la política, el perfil y los paquetes ---


def _plain(html: str) -> str:
    text_value = unescape(_TAG.sub(" ", html))
    text_value = _PLACEHOLDER.sub("[dato de la reserva]", text_value)
    return " ".join(text_value.split())


def _split_long(value: str) -> list[str]:
    """Parte un texto largo en trozos de hasta MAX_CHUNK_CHARS cortando por oraciones."""
    if len(value) <= MAX_CHUNK_CHARS:
        return [value]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", value):
        if current and len(current) + len(sentence) + 1 > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [p[:MAX_CHUNK_CHARS] for p in pieces]


def contract_chunks(profile: Any) -> list[Chunk]:
    """Secciones de la contrata del músico (la de Chivapp por defecto si no escribió una propia)."""
    from app.services.contract_pdf import (  # import tardío: módulo pesado
        apply_cancellation_policy_to_contract_html,
        build_default_contract_template_body,
        contract_html_text_length,
    )

    body = getattr(profile, "contract_template_body", None)
    if contract_html_text_length(body) < 50:
        body = build_default_contract_template_body()
    else:
        body = apply_cancellation_policy_to_contract_html(body)

    chunks: list[Chunk] = []
    for index, section in enumerate(_HEADING_SPLIT.split(body)):
        plain = _plain(section)
        if len(plain) < 20:
            continue
        # El encabezado queda como primera frase de la sección al quitar las etiquetas.
        heading = plain.split(".")[0][:60]
        for part_index, part in enumerate(_split_long(plain)):
            chunks.append(Chunk("contract", f"{index}.{part_index}:{heading}", part))
    return chunks


def policy_chunk() -> Chunk:
    full = int(settings.CANCEL_FULL_REFUND_MIN_DAYS)
    partial = int(settings.CANCEL_PARTIAL_REFUND_MIN_DAYS)
    pct = int(settings.CANCEL_PARTIAL_REFUND_PERCENT)
    content = (
        "Política de cancelación de Chivapp. Si el cliente cancela con "
        f"{full} días calendario o más de anticipación, se le reembolsa el 100 % de la tarifa del artista; "
        f"entre {partial} y {max(partial, full - 1)} días, el {pct} %; con menos de {partial} días no hay reembolso. "
        "La comisión de servicio y el costo de la pasarela de pagos no se reembolsan. "
        "Si el artista no puede asistir, se devuelve el 100 % de lo pagado. "
        "Los reembolsos los revisa y aprueba Chivapp. El pago es único, por el enlace de Chivapp (Mercado Pago)."
    )
    return Chunk("policy", "cancelacion", content)


def profile_chunks(profile: Any, config: Any) -> list[Chunk]:
    from app.services.musician_assistant import _show_facts  # import tardío: evita un ciclo

    chunks: list[Chunk] = []
    for fact in _show_facts(profile, config, {}):
        label = fact.split(":")[0][:60]
        chunks.append(Chunk("profile", label, fact[:MAX_CHUNK_CHARS]))
    for package in getattr(config, "packages", None) or []:
        if not isinstance(package, dict):
            continue
        name = str(package.get("name") or "").strip()
        if not name:
            continue
        description = str(package.get("description") or "").strip()
        detail = f"{package.get('members_count', '')} integrantes, {package.get('duration_hours', '')} h".strip(", ")
        content = f"Paquete «{name}» ({detail}). {description}".strip()
        chunks.append(Chunk("package", str(package.get("id") or name)[:100], content[:MAX_CHUNK_CHARS]))
    return chunks


def faq_chunks(entries: list[Any] | None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for entry in entries or []:
        content = ("Pregunta: " + entry.question + chr(10) + "Respuesta: " + entry.answer)[:MAX_CHUNK_CHARS]
        chunks.append(Chunk("faq", str(entry.id), content))
    return chunks


def load_entries(db: Session, profile: Any) -> list[Any]:
    from app.models.musician_assistant import AssistantKnowledgeEntry

    return (
        db.query(AssistantKnowledgeEntry)
        .filter(AssistantKnowledgeEntry.profile_id == profile.id)
        .order_by(AssistantKnowledgeEntry.created_at)
        .all()
    )


def build_chunks(profile: Any, config: Any, entries: list[Any] | None = None) -> list[Chunk]:
    """Todos los fragmentos vigentes del perfil, sin duplicados y con el tope por perfil.

    Lo que el músico enseñó va primero: si hay que recortar por el tope, se pierde antes lo genérico.
    """
    seen: set[tuple[str, str, str]] = set()
    result: list[Chunk] = []
    for chunk in [*faq_chunks(entries), *contract_chunks(profile), policy_chunk(), *profile_chunks(profile, config)]:
        key = (chunk.source, chunk.ref, chunk.hash)
        if key in seen:
            continue
        seen.add(key)
        result.append(chunk)
    return result[: int(settings.ASSISTANT_MAX_CHUNKS_PER_PROFILE)]


# --- Embeddings ---


def _embed_request(texts: list[str], task_type: str) -> list[list[float]]:
    api_key = (getattr(settings, "GEMINI_API_KEY", None) or "").strip()
    if not api_key:
        raise EmbeddingError("Sin GEMINI_API_KEY")
    model = settings.GEMINI_EMBED_MODEL
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": value}]},
                "taskType": task_type,
                "outputDimensionality": EMBED_DIMENSIONS,
            }
            for value in texts
        ]
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Fallo de red al calcular embeddings: {exc}") from exc
    if resp.status_code >= 400:
        raise EmbeddingError(f"Embeddings status={resp.status_code} body={resp.text[:200]}")
    vectors = [item.get("values") or [] for item in (resp.json().get("embeddings") or [])]
    if len(vectors) != len(texts) or any(len(v) != EMBED_DIMENSIONS for v in vectors):
        raise EmbeddingError("Respuesta de embeddings con forma inesperada")
    return vectors


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embeddings de varios textos, en lotes. Punto único de contacto con el servicio (se sustituye en pruebas)."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        vectors.extend(_embed_request(texts[start : start + _EMBED_BATCH], task_type))
    return vectors


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


# --- Almacén ---


def vector_available(db: Session) -> bool:
    """True si la tabla de fragmentos existe (es decir, la extensión pgvector quedó activa)."""
    try:
        return bool(db.execute(text("SELECT to_regclass('assistant_knowledge_chunk') IS NOT NULL")).scalar())
    except Exception:
        return False


def sync_profile_chunks(db: Session, profile: Any, config: Any) -> dict[str, int] | None:
    """Pone la tabla al día con el contenido actual. None si pgvector no está disponible.

    Trabaja en una sesión propia para no tocar la transacción de la conversación.
    Si no hay cupo de embeddings o falla el servicio, deja lo que ya estaba y lo devuelve en `skipped`.
    """
    if not vector_available(db):
        return None
    wanted = build_chunks(profile, config, load_entries(db, profile))
    profile_id = str(profile.id)
    with Session(bind=db.get_bind()) as session:
        rows = session.execute(
            text("SELECT id, source, source_ref, content_hash FROM assistant_knowledge_chunk WHERE profile_id = :p"),
            {"p": profile_id},
        ).all()
        existing = {(r.source, r.source_ref, r.content_hash): r.id for r in rows}
        wanted_keys = {(c.source, c.ref, c.hash) for c in wanted}
        stale = [chunk_id for key, chunk_id in existing.items() if key not in wanted_keys]
        new = [c for c in wanted if (c.source, c.ref, c.hash) not in existing]

        stats = {"added": 0, "removed": 0, "kept": len(wanted) - len(new), "skipped": 0}
        vectors: list[list[float]] = []
        if new:
            if not consume_embeddings(db, len(new)):
                stats["skipped"] = len(new)
                new = []
            else:
                try:
                    vectors = embed_texts([c.content for c in new], "RETRIEVAL_DOCUMENT")
                except EmbeddingError as exc:
                    logger.warning("No se pudieron calcular los embeddings del perfil %s: %s", profile_id, exc)
                    stats["skipped"] = len(new)
                    new = []
        if stale and (not stats["skipped"]):
            session.execute(text("DELETE FROM assistant_knowledge_chunk WHERE id = ANY(:ids)"), {"ids": stale})
            stats["removed"] = len(stale)
        for chunk, vector in zip(new, vectors):
            session.execute(
                text(
                    "INSERT INTO assistant_knowledge_chunk "
                    "(id, profile_id, source, source_ref, content, content_hash, embedding) "
                    "VALUES (:id, :p, :source, :ref, :content, :hash, CAST(:emb AS vector))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "p": profile_id,
                    "source": chunk.source,
                    "ref": chunk.ref[:120],
                    "content": chunk.content,
                    "hash": chunk.hash,
                    "emb": _literal(vector),
                },
            )
            stats["added"] += 1
        session.commit()
    return stats


def _context_fallback(profile: Any, config: Any, entries: list[Any] | None = None) -> list[dict[str, Any]]:
    """Todo el conocimiento como contexto, con tope de caracteres (sin búsqueda semántica)."""
    results: list[dict[str, Any]] = []
    used = 0
    for chunk in build_chunks(profile, config, entries):
        if used + len(chunk.content) > MAX_CONTEXT_CHARS:
            break
        used += len(chunk.content)
        results.append({"content": chunk.content, "source": chunk.source, "ref": chunk.ref, "score": None})
    return results


def search_knowledge(
    db: Session,
    profile: Any,
    config: Any,
    query: str,
    *,
    k: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
) -> tuple[list[dict[str, Any]], str]:
    """Fragmentos más cercanos a `query`. Devuelve (fragmentos, modo).

    `modo` es «vector» (búsqueda semántica), «todo» (pgvector no disponible o falló: se envía todo el conocimiento)
    o «vacio» (la búsqueda no encontró nada suficientemente cercano).
    """
    clean = (query or "").strip()
    entries = load_entries(db, profile)
    if not clean or not vector_available(db):
        return _context_fallback(profile, config, entries), "todo"
    try:
        stats = sync_profile_chunks(db, profile, config)
        if stats and stats.get("skipped"):
            # El índice quedó incompleto (sin cupo de embeddings o con un fallo): buscar solo en lo ya indexado
            # haría creer que no hay información. Se envía todo el conocimiento como contexto.
            return _context_fallback(profile, config, entries), "todo"
        if not consume_embeddings(db, 1):
            return _context_fallback(profile, config, entries), "todo"
        vector = embed_texts([clean], "RETRIEVAL_QUERY")[0]
        # Sesión propia: si la consulta falla no se aborta la transacción de la conversación.
        with Session(bind=db.get_bind()) as session:
            rows = session.execute(
                text(
                    "SELECT content, source, source_ref, 1 - (embedding <=> CAST(:q AS vector)) AS score "
                    "FROM assistant_knowledge_chunk WHERE profile_id = :p "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                ),
                {"q": _literal(vector), "p": str(profile.id), "k": k},
            ).all()
    except Exception as exc:  # pgvector o el modelo fallaron: el asistente no debe caerse
        logger.warning("Búsqueda semántica del asistente falló, se usa todo el conocimiento: %s", exc)
        return _context_fallback(profile, config, entries), "todo"
    found = [
        {"content": r.content, "source": r.source, "ref": r.source_ref, "score": round(float(r.score), 4)}
        for r in rows
        if float(r.score) >= min_score
    ]
    return found, ("vector" if found else "vacio")
