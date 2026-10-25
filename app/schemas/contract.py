from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, model_validator
from app.services.contract_pdf import compute_contract_body_sha256


class ContractOut(BaseModel):
    id: UUID
    booking_id: UUID
    title: str | None = None
    body: str | None = None
    context: dict[str, Any] | None = None
    body_sha256: str | None = None
    signed_body_sha256: str | None = None
    # Legacy PDF paths (contratos antiguos). Nuevos contratos generan PDF on-demand.
    contract_pdf_url: str | None = None
    contract_signed_pdf_url: str | None = None
    contractor_signature_url: str | None = None
    musician_signature_url: str | None = None
    contractor_signed: bool
    musician_signed: bool
    terms_accepted: bool
    terms_accepted_at: datetime | None
    contractor_sign_timestamp: datetime | None
    musician_sign_timestamp: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def populate_hashes(self) -> "ContractOut":
        ctx = self.context if isinstance(self.context, dict) else {}
        if not self.body_sha256 and (self.title or self.body):
            self.body_sha256 = str(
                ctx.get("__body_sha256__")
                or compute_contract_body_sha256(self.title, self.body)
            )
        if not self.signed_body_sha256 and ctx.get("__signed_body_sha256__"):
            self.signed_body_sha256 = str(ctx.get("__signed_body_sha256__"))
        return self
