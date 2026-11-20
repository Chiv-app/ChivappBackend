import pytest

pytestmark = pytest.mark.db


def test_security_headers_present_in_responses(client):
    """Verifica que las cabeceras de seguridad HTTP estén presentes en las respuestas."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-XSS-Protection") == "1; mode=block"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in response.headers.get("Permissions-Policy", "")
    assert "microphone=(self)" in response.headers.get("Permissions-Policy", "")
