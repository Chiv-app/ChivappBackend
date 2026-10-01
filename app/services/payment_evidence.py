"""Helpers para comprobantes de pago (uno o varios archivos por Payment)."""

from __future__ import annotations

from fastapi import HTTPException

from app.models.payment import Payment
from app.services.uploads import upload_exists


def assert_upload_exists(upload_url: str, label: str = "comprobante") -> None:
    if not upload_exists(upload_url):
        raise HTTPException(
            status_code=400,
            detail=f"No se encontró el archivo de {label}. Vuelve a subirlo.",
        )


def normalize_evidence_urls(
    *,
    payment_evidence_url: str | None = None,
    payment_evidence_urls: list[str] | None = None,
) -> list[str]:
    urls: list[str] = []
    for item in payment_evidence_urls or []:
        cleaned = (item or "").strip()
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    primary = (payment_evidence_url or "").strip()
    if primary and primary not in urls:
        urls.insert(0, primary)
    return urls


def assert_evidence_uploads(urls: list[str], *, require_at_least_one: bool = True) -> list[str]:
    if require_at_least_one and not urls:
        raise HTTPException(
            status_code=400,
            detail="Debes adjuntar al menos un comprobante",
        )
    for index, url in enumerate(urls, start=1):
        assert_upload_exists(url, f"comprobante {index}" if len(urls) > 1 else "comprobante")
    return urls


def apply_evidence_urls(payment: Payment, urls: list[str]) -> None:
    cleaned = [url for url in urls if url]
    payment.evidence_urls = cleaned or None
    payment.evidence_url = cleaned[0] if cleaned else None


def payment_evidence_list(payment: Payment) -> list[str]:
    urls: list[str] = []
    for item in payment.evidence_urls or []:
        cleaned = (item or "").strip()
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    primary = (payment.evidence_url or "").strip()
    if primary and primary not in urls:
        urls.insert(0, primary)
    return urls
