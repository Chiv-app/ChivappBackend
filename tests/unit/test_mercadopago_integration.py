import hashlib
import hmac
from unittest.mock import patch

from app.core.config import settings
from app.services.mercadopago_service import verify_webhook_signature, get_webhook_url


def test_verify_webhook_signature_success():
    secret = "test_webhook_secret_123"
    data_id = "9988776655"
    x_request_id = "req-abc-123"
    ts = "1700000000"

    template = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    computed_v1 = hmac.new(secret.encode(), template.encode(), hashlib.sha256).hexdigest()
    x_signature = f"ts={ts},v1={computed_v1}"

    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_SECRET", secret):
        result = verify_webhook_signature(
            x_signature=x_signature,
            x_request_id=x_request_id,
            data_id=data_id,
        )
        assert result is True


def test_verify_webhook_signature_invalid():
    secret = "test_webhook_secret_123"
    data_id = "9988776655"
    x_request_id = "req-abc-123"
    x_signature = "ts=1700000000,v1=invalidsignature00000000"

    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_SECRET", secret):
        result = verify_webhook_signature(
            x_signature=x_signature,
            x_request_id=x_request_id,
            data_id=data_id,
        )
        assert result is False


def test_verify_webhook_signature_empty_secret_allowed_only_in_sandbox():
    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_SECRET", ""), patch.object(
        settings, "MERCADO_PAGO_SANDBOX", True
    ):
        assert verify_webhook_signature(x_signature=None, x_request_id=None, data_id="123456") is True


def test_verify_webhook_signature_empty_secret_rejected_in_production():
    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_SECRET", ""), patch.object(
        settings, "MERCADO_PAGO_SANDBOX", False
    ):
        assert verify_webhook_signature(x_signature=None, x_request_id=None, data_id="123456") is False


def test_get_webhook_url_uses_configured_base():
    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_BASE_URL", "https://api.chiv.app"):
        assert get_webhook_url() == "https://api.chiv.app/api/v1/payments/mercadopago/webhook"

    with patch.object(settings, "MERCADO_PAGO_WEBHOOK_BASE_URL", "https://api.chiv.app/api/v1"):
        assert get_webhook_url() == "https://api.chiv.app/api/v1/payments/mercadopago/webhook"
